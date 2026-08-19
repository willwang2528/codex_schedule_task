#!/usr/bin/env python3
"""Validate Automation Hub task definitions without third-party packages."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from feishu_cards import PRESENTATION_RULES


TASK_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_SECTIONS = (
    "schedule",
    "workflow",
    "task_prompt",
    "delivery",
    "output",
    "state",
)
TRIGGER_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ALLOWED_DELIVERY_TYPES = {"feishu", "none"}
ALLOWED_DELIVERY_POLICIES = {"always", "conditional", "never"}
ALLOWED_DELIVERY_PRESENTATIONS = {"post", *PRESENTATION_RULES}
ALLOWED_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
SECRET_KEY_MARKERS = ("secret", "password", "access_token", "private_key")
TASK_CHAT_ID_ENV_PATTERN = re.compile(
    r"^FEISHU_CHAT_ID_[A-Z0-9_]+_SCHEDULE_TASK$"
)


class TaskConfigError(ValueError):
    """Raised when a task configuration cannot be parsed or validated."""


def _parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise TaskConfigError(f"invalid inline JSON value: {value}") from exc
    if value.startswith(('"', "'")):
        if value.startswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise TaskConfigError(f"invalid quoted value: {value}") from exc
        if len(value) < 2 or not value.endswith("'"):
            raise TaskConfigError(f"invalid quoted value: {value}")
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def load_task_config(path: Path) -> Dict[str, Any]:
    """Parse the mapping-only YAML subset used by Automation Hub task files."""

    root: Dict[str, Any] = {}
    stack = [(-1, root)]

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TaskConfigError(f"cannot read task config: {path}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise TaskConfigError(f"line {line_number}: tabs are not allowed")

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2:
            raise TaskConfigError(
                f"line {line_number}: indentation must use multiples of two spaces"
            )

        content = raw_line.strip()
        if ":" not in content:
            raise TaskConfigError(f"line {line_number}: expected 'key: value'")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        if not key:
            raise TaskConfigError(f"line {line_number}: empty key")

        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise TaskConfigError(f"line {line_number}: duplicate key '{key}'")

        if raw_value.strip():
            parent[key] = _parse_scalar(raw_value)
        else:
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))

    return root


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _nested(config: Dict[str, Any], section: str, key: str) -> Any:
    value = config.get(section)
    if not isinstance(value, dict):
        return None
    return value.get(key)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    )


def _iter_values(value: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        yield path, child
        if isinstance(child, dict):
            yield from _iter_values(child, path)


def schedule_triggers(config: Dict[str, Any]) -> List[str]:
    """Return normalized HH:MM triggers from current or legacy fields."""

    schedule = config.get("schedule")
    if not isinstance(schedule, dict):
        return []
    triggers = schedule.get("triggers")
    if isinstance(triggers, list):
        return [str(value) for value in triggers]
    cron = schedule.get("cron")
    if not isinstance(cron, str):
        return []
    fields = cron.split()
    if len(fields) != 5:
        return []
    minute, hour = fields[0], fields[1]
    if minute.isdigit() and hour.isdigit():
        return [f"{int(hour):02d}:{int(minute):02d}"]
    return []


def discover_task_configs(repo_root: Path) -> List[Path]:
    """Discover concrete task definitions in deterministic order."""

    tasks_root = repo_root / "tasks"
    if not tasks_root.is_dir():
        return []
    return sorted(
        path
        for path in tasks_root.glob("*/task.yaml")
        if path.parent.name != "_template"
    )


def validate_all_tasks(repo_root: Path) -> List[Dict[str, Any]]:
    """Validate every task and enforce repository-wide unique ids."""

    reports: List[Dict[str, Any]] = []
    ids: Dict[str, str] = {}
    for config_path in discover_task_configs(repo_root):
        try:
            config = load_task_config(config_path)
            errors = validate_task_config(config, config_path, repo_root)
        except TaskConfigError as exc:
            config = {}
            errors = [str(exc)]
        task_id = config.get("id")
        if isinstance(task_id, str):
            if task_id in ids:
                errors.append(f"duplicate task id also used by {ids[task_id]}")
            else:
                ids[task_id] = str(config_path.relative_to(repo_root))
        reports.append(
            {
                "task": task_id,
                "config": str(config_path.relative_to(repo_root)),
                "valid": not errors,
                "errors": errors,
            }
        )
    return reports


def validate_task_config(
    config: Dict[str, Any], config_path: Path, repo_root: Path
) -> List[str]:
    errors: List[str] = []
    task_id = config.get("id")

    if not _is_relative_to(config_path.resolve(), repo_root.resolve()):
        errors.append("task config must stay inside the repository")

    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        errors.append("id must use lowercase letters, digits, and hyphens")
    elif config_path.parent.name != "_template" and task_id != config_path.parent.name:
        errors.append("id must match the task directory name")

    if not isinstance(config.get("name"), str) or not config.get("name", "").strip():
        errors.append("name must be a non-empty string")
    if not isinstance(config.get("enabled"), bool):
        errors.append("enabled must be true or false")

    for section in REQUIRED_SECTIONS:
        if not isinstance(config.get(section), dict):
            errors.append(f"{section} must be a mapping")

    timezone_name = _nested(config, "schedule", "timezone")
    cron = _nested(config, "schedule", "cron")
    triggers = _nested(config, "schedule", "triggers")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        errors.append("schedule.timezone must be a non-empty string")
    else:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append("schedule.timezone must be a valid IANA timezone")
    if triggers is None:
        if not isinstance(cron, str) or len(cron.split()) != 5:
            errors.append(
                "schedule must define triggers or a quoted five-field cron string"
            )
    elif not isinstance(triggers, list) or not triggers:
        errors.append("schedule.triggers must be a non-empty inline list")
    else:
        normalized = [str(value) for value in triggers]
        if any(not TRIGGER_PATTERN.fullmatch(value) for value in normalized):
            errors.append("schedule.triggers values must use HH:MM (24-hour time)")
        if len(normalized) != len(set(normalized)):
            errors.append("schedule.triggers must not contain duplicates")
    catch_up_minutes = _nested(config, "schedule", "catch_up_minutes")
    if catch_up_minutes is not None and not _positive_int(catch_up_minutes):
        errors.append("schedule.catch_up_minutes must be a positive integer")

    workflow_type = _nested(config, "workflow", "type")
    if not isinstance(workflow_type, str) or not workflow_type.strip():
        errors.append("workflow.type must be a non-empty string")
    if workflow_type == "research_topk":
        top_k = _nested(config, "workflow", "top_k")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            errors.append("workflow.top_k must be a positive integer")

    path_fields = (
        ("task_prompt", "path", True),
        ("output", "directory", False),
        ("state", "path", False),
    )
    for section, key, must_exist in path_fields:
        value = _nested(config, section, key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{section}.{key} must be a non-empty relative path")
            continue
        candidate = (repo_root / value).resolve()
        if not _is_relative_to(candidate, repo_root):
            errors.append(f"{section}.{key} must stay inside the repository")
        elif must_exist and not candidate.is_file():
            errors.append(f"{section}.{key} does not exist: {value}")

    deterministic_script = _nested(config, "workflow", "deterministic_script")
    if deterministic_script is not None:
        if not isinstance(deterministic_script, str) or not deterministic_script.strip():
            errors.append("workflow.deterministic_script must be a relative path")
        else:
            candidate = (repo_root / deterministic_script).resolve()
            scripts_root = (repo_root / "scripts").resolve()
            if not _is_relative_to(candidate, scripts_root):
                errors.append("workflow.deterministic_script must stay inside scripts/")
            elif not candidate.is_file():
                errors.append(
                    f"workflow.deterministic_script does not exist: {deterministic_script}"
                )

    context_script = _nested(config, "workflow", "context_script")
    if context_script is not None:
        if not isinstance(context_script, str) or not context_script.strip():
            errors.append("workflow.context_script must be a relative path")
        else:
            candidate = (repo_root / context_script).resolve()
            tasks_root = (repo_root / "tasks").resolve()
            if not _is_relative_to(candidate, tasks_root):
                errors.append("workflow.context_script must stay inside tasks/")
            elif not candidate.is_file():
                errors.append(
                    f"workflow.context_script does not exist: {context_script}"
                )
    context_timeout = _nested(config, "workflow", "context_timeout_seconds")
    if context_timeout is not None and not _positive_int(context_timeout):
        errors.append("workflow.context_timeout_seconds must be a positive integer")

    delivery_type = _nested(config, "delivery", "type")
    if delivery_type not in ALLOWED_DELIVERY_TYPES:
        errors.append("delivery.type must be 'feishu' or 'none'")
    if not isinstance(_nested(config, "delivery", "enabled"), bool):
        errors.append("delivery.enabled must be true or false")
    if not isinstance(_nested(config, "output", "save_local"), bool):
        errors.append("output.save_local must be true or false")
    if not isinstance(_nested(config, "state", "enabled"), bool):
        errors.append("state.enabled must be true or false")

    delivery_policy = _nested(config, "delivery", "policy")
    if delivery_policy is not None and delivery_policy not in ALLOWED_DELIVERY_POLICIES:
        errors.append("delivery.policy must be always, conditional, or never")
    delivery_target = _nested(config, "delivery", "target")
    if delivery_target is not None and delivery_target != "configured_chat":
        errors.append("delivery.target must be configured_chat")
    delivery_chat_id_env = _nested(config, "delivery", "chat_id_env")
    if delivery_chat_id_env is not None and (
        not isinstance(delivery_chat_id_env, str)
        or not TASK_CHAT_ID_ENV_PATTERN.fullmatch(delivery_chat_id_env)
    ):
        errors.append(
            "delivery.chat_id_env must use FEISHU_CHAT_ID_<TASK>_SCHEDULE_TASK"
        )
    notification_triggers = _nested(config, "delivery", "notification_triggers")
    if notification_triggers is not None:
        if not isinstance(notification_triggers, list) or not notification_triggers:
            errors.append("delivery.notification_triggers must be a non-empty inline list")
        else:
            normalized_notification_triggers = [
                str(value) for value in notification_triggers
            ]
            if any(
                not TRIGGER_PATTERN.fullmatch(value)
                for value in normalized_notification_triggers
            ):
                errors.append(
                    "delivery.notification_triggers values must use HH:MM (24-hour time)"
                )
            if len(normalized_notification_triggers) != len(
                set(normalized_notification_triggers)
            ):
                errors.append("delivery.notification_triggers must not contain duplicates")
            schedule_trigger_values = set(schedule_triggers(config))
            unknown_notification_triggers = sorted(
                set(normalized_notification_triggers).difference(schedule_trigger_values)
            )
            if unknown_notification_triggers:
                errors.append(
                    "delivery.notification_triggers must be a subset of schedule.triggers: "
                    + ", ".join(unknown_notification_triggers)
                )
    delivery_retry = _nested(config, "delivery", "retry_attempts")
    if delivery_retry is not None and not _positive_int(delivery_retry):
        errors.append("delivery.retry_attempts must be a positive integer")
    delivery_presentation = _nested(config, "delivery", "presentation")
    if (
        delivery_presentation is not None
        and delivery_presentation not in ALLOWED_DELIVERY_PRESENTATIONS
    ):
        errors.append("delivery.presentation is invalid")

    execution = config.get("execution")
    if execution is not None and not isinstance(execution, dict):
        errors.append("execution must be a mapping")
    elif isinstance(execution, dict):
        timeout_seconds = execution.get("timeout_seconds")
        if timeout_seconds is not None and not _positive_int(timeout_seconds):
            errors.append("execution.timeout_seconds must be a positive integer")
        retry_attempts = execution.get("retry_attempts")
        if retry_attempts is not None and not _positive_int(retry_attempts):
            errors.append("execution.retry_attempts must be a positive integer")
        retry_backoff = execution.get("retry_backoff_seconds")
        if retry_backoff is not None and not _nonnegative_number(retry_backoff):
            errors.append(
                "execution.retry_backoff_seconds must be a non-negative number"
            )
        model = execution.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            errors.append("execution.model must be a non-empty string")
        reasoning_effort = execution.get("reasoning_effort")
        if (
            reasoning_effort is not None
            and reasoning_effort not in ALLOWED_REASONING_EFFORTS
        ):
            errors.append("execution.reasoning_effort is invalid")

    logging = config.get("logging")
    if logging is not None and not isinstance(logging, dict):
        errors.append("logging must be a mapping")
    elif isinstance(logging, dict):
        directory = logging.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            errors.append("logging.directory must be a non-empty relative path")
        else:
            candidate = (repo_root / directory).resolve()
            if not _is_relative_to(candidate, repo_root):
                errors.append("logging.directory must stay inside the repository")

    for path, value in _iter_values(config):
        final_key = path.rsplit(".", 1)[-1].lower()
        if any(marker in final_key for marker in SECRET_KEY_MARKERS):
            if value is not None and value != "":
                errors.append(f"{path} must not contain a hard-coded secret")

    state_value = _nested(config, "state", "path")
    if isinstance(state_value, str) and state_value.strip():
        state_candidate = (repo_root / state_value).resolve()
        parent = state_candidate.parent
        while not parent.exists() and parent != repo_root.parent:
            parent = parent.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            errors.append("state.path parent is not writable")

    return errors


def resolve_task_config(task_reference: str, repo_root: Path) -> Path:
    reference_path = Path(task_reference)
    if reference_path.suffix in {".yaml", ".yml"} or reference_path.is_absolute():
        return reference_path.resolve()
    return (repo_root / "tasks" / task_reference / "task.yaml").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="Task id or path to task.yaml")
    parser.add_argument(
        "--all", action="store_true", help="Validate every discovered task"
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    if args.all:
        reports = validate_all_tasks(repo_root)
        result = {
            "valid": bool(reports) and all(report["valid"] for report in reports),
            "task_count": len(reports),
            "tasks": reports,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["valid"] else 1
    if not args.task:
        parser.error("task is required unless --all is used")
    config_path = resolve_task_config(args.task, repo_root)
    try:
        config = load_task_config(config_path)
        errors = validate_task_config(config, config_path, repo_root)
    except TaskConfigError as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1

    result = {
        "valid": not errors,
        "task": config.get("id"),
        "config": (
            str(config_path.relative_to(repo_root))
            if _is_relative_to(config_path, repo_root)
            else str(config_path)
        ),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
