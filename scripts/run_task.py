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


def _sanitize_error(message: str) -> str:
    sanitized = message
    for key, value in os.environ.items():
        if value and any(marker in key.upper() for marker in SECRET_MARKERS):
            sanitized = sanitized.replace(value, "[REDACTED]")
    return sanitized.strip()[:2000]


def _append_log(log_path: Path, record: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


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
    args = parser.parse_args()

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
    started = _now(timezone_name)
    output_dir = (repo_root / str(config["output"]["directory"])).resolve()
    state_path = (repo_root / str(config["state"]["path"])).resolve()
    prompt_path = (repo_root / str(config["task_prompt"]["path"])).resolve()
    log_path = repo_root / "logs" / task_id / f"{started.date().isoformat()}.log"

    output_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if config["state"]["enabled"] and not state_path.exists():
        state_path.write_text("{}\n", encoding="utf-8")

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
    result.setdefault("status", status)
    result.setdefault("delivery_status", delivery_status)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
