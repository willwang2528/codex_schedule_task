#!/usr/bin/env python3
"""Validate Automation Hub task definitions without third-party packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TASK_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_SECTIONS = (
    "schedule",
    "workflow",
    "task_prompt",
    "delivery",
    "output",
    "state",
)


class TaskConfigError(ValueError):
    """Raised when a task configuration cannot be parsed or validated."""


def _parse_scalar(raw_value: str) -> Any:
    value = raw_value.strip()
    if not value:
        return ""
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
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        errors.append("schedule.timezone must be a non-empty string")
    else:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            errors.append("schedule.timezone must be a valid IANA timezone")
    if not isinstance(cron, str) or len(cron.split()) != 5:
        errors.append("schedule.cron must be a quoted five-field cron string")

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

    delivery_type = _nested(config, "delivery", "type")
    if delivery_type not in {"feishu", "none"}:
        errors.append("delivery.type must be 'feishu' or 'none'")
    if not isinstance(_nested(config, "delivery", "enabled"), bool):
        errors.append("delivery.enabled must be true or false")
    if not isinstance(_nested(config, "output", "save_local"), bool):
        errors.append("output.save_local must be true or false")
    if not isinstance(_nested(config, "state", "enabled"), bool):
        errors.append("state.enabled must be true or false")

    return errors


def resolve_task_config(task_reference: str, repo_root: Path) -> Path:
    reference_path = Path(task_reference)
    if reference_path.suffix in {".yaml", ".yml"} or reference_path.is_absolute():
        return reference_path.resolve()
    return (repo_root / "tasks" / task_reference / "task.yaml").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="Task id or path to task.yaml")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
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
