#!/usr/bin/env python3
"""Discover enabled Automation Hub tasks and execute due trigger slots."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from task_runtime import TaskRuntimeError, make_run_id, read_json_object
from validate_task import (
    TaskConfigError,
    discover_task_configs,
    load_task_config,
    schedule_triggers,
    validate_task_config,
)


def _parse_at(value: Optional[str]) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TaskConfigError("--at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise TaskConfigError("--at must include an explicit timezone")
    return parsed


def discover_tasks(repo_root: Path) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    seen_ids = set()
    for config_path in discover_task_configs(repo_root):
        config = load_task_config(config_path)
        errors = validate_task_config(config, config_path, repo_root)
        if errors:
            raise TaskConfigError(
                f"{config_path.relative_to(repo_root)}: " + "; ".join(errors)
            )
        task_id = str(config["id"])
        if task_id in seen_ids:
            raise TaskConfigError(f"duplicate task id: {task_id}")
        seen_ids.add(task_id)
        tasks.append(
            {
                "id": task_id,
                "name": str(config["name"]),
                "enabled": bool(config["enabled"]),
                "timezone": str(config["schedule"]["timezone"]),
                "triggers": schedule_triggers(config),
                "catch_up_minutes": int(
                    config["schedule"].get("catch_up_minutes", 15)
                ),
                "config_path": str(config_path.relative_to(repo_root)),
                "state_path": str(config["state"]["path"]),
            }
        )
    return tasks


def due_runs(repo_root: Path, at: datetime) -> List[Dict[str, Any]]:
    due: List[Dict[str, Any]] = []
    for task in discover_tasks(repo_root):
        if not task["enabled"]:
            continue
        timezone = ZoneInfo(str(task["timezone"]))
        local_now = at.astimezone(timezone)
        for trigger in task["triggers"]:
            hour, minute = (int(part) for part in str(trigger).split(":"))
            scheduled = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            age = local_now - scheduled
            if timedelta(0) <= age <= timedelta(minutes=task["catch_up_minutes"]):
                state_path = (repo_root / str(task["state_path"])).resolve()
                state = read_json_object(state_path)
                runtime = state.get("_runtime")
                processed = (
                    runtime.get("processed_runs")
                    if isinstance(runtime, dict)
                    else None
                )
                scheduled_text = scheduled.isoformat(timespec="seconds")
                run_id = make_run_id(str(task["id"]), scheduled_text, str(trigger))
                prior = processed.get(run_id) if isinstance(processed, dict) else None
                if isinstance(prior, dict) and prior.get("terminal"):
                    continue
                due.append(
                    {
                        "task": task["id"],
                        "name": task["name"],
                        "scheduled_at": scheduled_text,
                        "trigger_slot": trigger,
                        "timezone": task["timezone"],
                    }
                )
    return sorted(due, key=lambda item: (item["scheduled_at"], item["task"]))


def _has_pending_delivery(repo_root: Path, task: Dict[str, Any]) -> bool:
    state_path = (repo_root / str(task["state_path"])).resolve()
    state = read_json_object(state_path)
    runtime = state.get("_runtime")
    notifications = runtime.get("notifications") if isinstance(runtime, dict) else None
    return isinstance(notifications, dict) and any(
        isinstance(value, dict) and value.get("status") == "pending"
        for value in notifications.values()
    )


def _run_command(command: List[str], repo_root: Path) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    value: Dict[str, Any] = {
        "return_code": completed.returncode,
        "command_task": command[2] if len(command) > 2 else None,
    }
    for line in reversed(completed.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            value.update(parsed)
            break
    if completed.returncode != 0 and "error" not in value:
        value["error"] = " ".join(
            (completed.stderr or completed.stdout or "task command failed").split()
        )[:2000]
    return value


def run_once(
    repo_root: Path,
    *,
    at: datetime,
    dry_run: bool,
    recover_pending: bool,
) -> Dict[str, Any]:
    tasks = discover_tasks(repo_root)
    due = due_runs(repo_root, at)
    results: List[Dict[str, Any]] = []
    if dry_run:
        return {"status": "ok", "at": at.isoformat(), "due": due, "results": []}

    runner = repo_root / "scripts" / "run_task.py"
    if recover_pending:
        for task in tasks:
            if task["enabled"] and _has_pending_delivery(repo_root, task):
                results.append(
                    _run_command(
                        [sys.executable, str(runner), task["id"], "--recover-pending"],
                        repo_root,
                    )
                )
    for run in due:
        results.append(
            _run_command(
                [
                    sys.executable,
                    str(runner),
                    str(run["task"]),
                    "--execute-agent",
                    "--scheduled-at",
                    str(run["scheduled_at"]),
                    "--trigger-slot",
                    str(run["trigger_slot"]),
                ],
                repo_root,
            )
        )
    failed = [result for result in results if result.get("return_code") != 0]
    return {
        "status": "failed" if failed else "ok",
        "at": at.isoformat(),
        "due": due,
        "results": results,
        "failed_count": len(failed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List discovered tasks")
    parser.add_argument("--run-due", action="store_true", help="Run due task slots once")
    parser.add_argument("--daemon", action="store_true", help="Poll and run due slots continuously")
    parser.add_argument("--at", help="Timezone-aware ISO time for deterministic checks")
    parser.add_argument("--dry-run", action="store_true", help="Show due runs without executing")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--no-recovery", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    if sum((args.list, args.run_due, args.daemon)) != 1:
        parser.error("choose exactly one of --list, --run-due, or --daemon")
    try:
        if args.list:
            tasks = discover_tasks(repo_root)
            print(json.dumps({"task_count": len(tasks), "tasks": tasks}, ensure_ascii=False, sort_keys=True))
            return 0
        if args.poll_seconds < 10:
            parser.error("--poll-seconds must be at least 10")
        if args.run_due:
            result = run_once(
                repo_root,
                at=_parse_at(args.at),
                dry_run=args.dry_run,
                recover_pending=not args.no_recovery,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 1 if result["status"] == "failed" else 0

        while True:
            result = run_once(
                repo_root,
                at=datetime.now().astimezone(),
                dry_run=False,
                recover_pending=not args.no_recovery,
            )
            if result["due"] or result["results"] or result["status"] == "failed":
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except (OSError, TaskConfigError, TaskRuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
