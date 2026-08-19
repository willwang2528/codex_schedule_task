#!/usr/bin/env python3
"""Thin, task-driven entrypoint for Automation Hub."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from production_runner import execute_production_task, recover_pending_delivery
from task_runtime import (
    FAILED,
    SKIPPED,
    TaskRuntimeError,
    append_jsonl,
    atomic_write_json,
)
from validate_task import (
    TaskConfigError,
    load_task_config,
    resolve_task_config,
    validate_task_config,
)


SECRET_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY")


def _now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise TaskConfigError(f"unknown timezone: {timezone_name}") from exc


def _scheduled_datetime(value: Optional[str], timezone_name: str) -> datetime:
    if value is None:
        return _now(timezone_name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TaskConfigError("scheduled_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise TaskConfigError("scheduled_at must include an explicit timezone")
    return parsed.astimezone(ZoneInfo(timezone_name))


def _sanitize_error(message: str) -> str:
    sanitized = message
    for key, value in os.environ.items():
        if value and any(marker in key.upper() for marker in SECRET_MARKERS):
            sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized.strip()[:2000]


def _append_log(log_path: Path, record: Dict[str, Any]) -> None:
    append_jsonl(log_path, record)


def _last_json_line(output: str) -> Optional[Dict[str, Any]]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="Task id or path to task.yaml")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate and prepare paths without running a deterministic executor",
    )
    parser.add_argument(
        "--execute-agent",
        action="store_true",
        help="Run a structured Codex agent and finalize output/state/delivery",
    )
    parser.add_argument(
        "--result-file",
        type=Path,
        help="Finalize a precomputed structured result instead of invoking Codex",
    )
    parser.add_argument(
        "--scheduled-at",
        help="Timezone-aware ISO-8601 scheduled time used for idempotent run identity",
    )
    parser.add_argument("--trigger-slot", default="manual", help="Schedule slot label")
    parser.add_argument(
        "--dry-run-delivery",
        action="store_true",
        help="Evaluate notification policy without calling Feishu",
    )
    parser.add_argument(
        "--recover-pending",
        action="store_true",
        help="Retry pending idempotent deliveries without rerunning the Agent",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow a disabled task or an already completed run to execute manually",
    )
    args = parser.parse_args()

    action_count = sum(
        bool(value)
        for value in (
            args.prepare_only,
            args.execute_agent,
            args.result_file,
            args.recover_pending,
        )
    )
    if action_count > 1:
        parser.error(
            "choose only one of --prepare-only, --execute-agent, --result-file, "
            "or --recover-pending"
        )

    repo_root = Path(__file__).resolve().parents[1]
    config_path = resolve_task_config(args.task, repo_root)

    try:
        config = load_task_config(config_path)
        errors = validate_task_config(config, config_path, repo_root)
        if errors:
            raise TaskConfigError("; ".join(errors))
    except TaskConfigError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1

    task_id = str(config["id"])
    timezone_name = str(config["schedule"]["timezone"])
    try:
        scheduled_at = _scheduled_datetime(args.scheduled_at, timezone_name)
    except TaskConfigError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    started = _now(timezone_name)
    output_dir = (repo_root / str(config["output"]["directory"])).resolve()
    state_path = (repo_root / str(config["state"]["path"])).resolve()
    prompt_path = (repo_root / str(config["task_prompt"]["path"])).resolve()
    logging_config = config.get("logging")
    logging_directory = (
        str(logging_config.get("directory"))
        if isinstance(logging_config, dict) and logging_config.get("directory")
        else f"logs/{task_id}"
    )
    log_path = repo_root / logging_directory / f"{started.date().isoformat()}.log"

    output_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if config["state"]["enabled"] and not state_path.exists():
        atomic_write_json(state_path, {})

    base_result: Dict[str, Any] = {
        "task": task_id,
        "config_path": str(config_path.relative_to(repo_root)),
        "prompt_path": str(prompt_path.relative_to(repo_root)),
        "output_directory": str(output_dir.relative_to(repo_root)),
        "state_path": str(state_path.relative_to(repo_root)),
        "log_path": str(log_path.relative_to(repo_root)),
        "enabled": config["enabled"],
    }

    deterministic_script = config["workflow"].get("deterministic_script")
    production_action = args.execute_agent or args.result_file is not None
    if not config["enabled"] and (production_action or args.recover_pending) and not args.force:
        ended = _now(timezone_name)
        result = {
            **base_result,
            "status": SKIPPED,
            "skip_reason": "task_disabled",
            "delivery_status": "not_started",
            "notification_sent": False,
            "error": None,
        }
        _append_log(
            log_path,
            {
                "task_id": task_id,
                "run_id": None,
                "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": ended.isoformat(timespec="seconds"),
                "status": SKIPPED,
                "trigger_slot": args.trigger_slot,
                "state_version": None,
                "notification_sent": False,
                "delivery_status": "not_started",
                "output_path": None,
                "error": None,
            },
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.recover_pending:
        try:
            recovery = recover_pending_delivery(
                config=config,
                state_path=state_path,
                dry_run_delivery=args.dry_run_delivery,
            )
        except (OSError, TaskRuntimeError) as exc:
            recovery = {
                "status": FAILED,
                "recovered": 0,
                "failed": 1,
                "error": _sanitize_error(str(exc)),
            }
        ended = _now(timezone_name)
        result = {**base_result, **recovery}
        _append_log(
            log_path,
            {
                "task_id": task_id,
                "run_id": None,
                "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": ended.isoformat(timespec="seconds"),
                "status": recovery["status"],
                "trigger_slot": "recovery",
                "state_version": None,
                "notification_sent": bool(recovery.get("recovered")),
                "delivery_status": "recovered" if recovery.get("recovered") else "none",
                "output_path": None,
                "error": recovery.get("error"),
            },
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if recovery["status"] == FAILED else 0

    if production_action:
        try:
            production_result = execute_production_task(
                repo_root=repo_root,
                config=config,
                prompt_path=prompt_path,
                state_path=state_path,
                output_directory=output_dir,
                scheduled_at=scheduled_at,
                trigger_slot=args.trigger_slot,
                result_file=args.result_file.resolve() if args.result_file else None,
                dry_run_delivery=args.dry_run_delivery,
                force=args.force,
            )
        except (OSError, TaskRuntimeError) as exc:
            production_result = {
                "task": task_id,
                "run_id": None,
                "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
                "trigger_slot": args.trigger_slot,
                "status": FAILED,
                "state_version": None,
                "notification_sent": False,
                "delivery_status": "not_started",
                "output_json": None,
                "output_markdown": None,
                "error": _sanitize_error(str(exc)),
            }
        ended = _now(timezone_name)
        result = {**base_result, **production_result}
        _append_log(
            log_path,
            {
                "task_id": task_id,
                "run_id": production_result.get("run_id"),
                "scheduled_at": production_result.get("scheduled_at"),
                "started_at": started.isoformat(timespec="seconds"),
                "finished_at": ended.isoformat(timespec="seconds"),
                "status": production_result["status"],
                "trigger_slot": production_result.get("trigger_slot", args.trigger_slot),
                "state_version": production_result.get("state_version"),
                "notification_sent": production_result.get("notification_sent", False),
                "delivery_status": production_result.get("delivery_status"),
                "output_path": production_result.get("output_markdown"),
                "error": production_result.get("error"),
            },
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if production_result["status"] == FAILED else 0

    if args.prepare_only or not deterministic_script:
        ended = _now(timezone_name)
        result = {
            **base_result,
            "status": "ready_for_codex",
            "delivery_status": "not_started",
        }
        _append_log(
            log_path,
            {
                "task": task_id,
                "start_time": started.isoformat(),
                "end_time": ended.isoformat(),
                "status": result["status"],
                "output_path": None,
                "delivery_status": result["delivery_status"],
                "error": None,
            },
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    executor_path = (repo_root / str(deterministic_script)).resolve()
    child_env = os.environ.copy()
    child_env.update(
        {
            "AUTOMATION_HUB_ROOT": str(repo_root),
            "AUTOMATION_TASK_ID": task_id,
            "AUTOMATION_OUTPUT_DIR": str(output_dir),
            "AUTOMATION_STATE_PATH": str(state_path),
            "AUTOMATION_TIMEZONE": timezone_name,
            "AUTOMATION_DELIVERY_ENABLED": str(
                config["delivery"]["enabled"]
            ).lower(),
        }
    )

    child_summary: Dict[str, Any] = {}
    error: Optional[str] = None
    return_code = 1
    try:
        completed = subprocess.run(
            [sys.executable, str(executor_path)],
            cwd=repo_root,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return_code = completed.returncode
        child_summary = _last_json_line(completed.stdout) or {}
        if return_code != 0:
            error = _sanitize_error(
                completed.stderr or completed.stdout or "deterministic executor failed"
            )
    except OSError as exc:
        error = _sanitize_error(str(exc))

    ended = _now(timezone_name)
    status = str(child_summary.get("status") or ("failed" if error else "ok"))
    delivery_status = str(child_summary.get("delivery_status", "not_started"))
    output_path = child_summary.get("output_path")
    record = {
        "task": task_id,
        "start_time": started.isoformat(),
        "end_time": ended.isoformat(),
        "status": status,
        "output_path": output_path,
        "delivery_status": delivery_status,
        "error": error,
    }
    _append_log(log_path, record)

    result = {**base_result, **child_summary, "error": error}
    if error is None and child_summary.get("delivery_error"):
        result["error"] = _sanitize_error(str(child_summary["delivery_error"]))
    result.setdefault("status", status)
    result.setdefault("delivery_status", delivery_status)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
