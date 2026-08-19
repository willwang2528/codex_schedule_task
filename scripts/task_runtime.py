#!/usr/bin/env python3
"""Shared transactional runtime helpers for Automation Hub tasks."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from feishu_cards import CardSpecError, validate_card_specs


SUCCESS_NOTIFY = "SUCCESS_NOTIFY"
SUCCESS_NO_NOTIFY = "SUCCESS_NO_NOTIFY"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
RESULT_STATUSES = {
    SUCCESS_NOTIFY,
    SUCCESS_NO_NOTIFY,
    SKIPPED,
    FAILED,
}


class TaskRuntimeError(RuntimeError):
    """Raised when a run cannot be safely prepared or finalized."""


def now_in(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def read_json_object(path: Path, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default) if default is not None else {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskRuntimeError(f"cannot read valid JSON state: {path}") from exc
    if not isinstance(value, dict):
        raise TaskRuntimeError(f"state root must be a JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    """Write JSON through a same-directory temp file and atomic rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def deep_merge(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge mappings; lists and scalar values replace atomically."""

    merged = copy.deepcopy(base)
    for key, value in updates.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _compact(value: Any, *, max_items: int, depth: int = 0) -> Any:
    if depth >= 6:
        return "[depth-limited]"
    if isinstance(value, dict):
        items = list(value.items())
        if len(items) > max_items:
            items = items[-max_items:]
        return {
            str(key): _compact(child, max_items=max_items, depth=depth + 1)
            for key, child in items
        }
    if isinstance(value, list):
        return [
            _compact(child, max_items=max_items, depth=depth + 1)
            for child in value[-max_items:]
        ]
    if isinstance(value, str) and len(value) > 1200:
        return value[:1200] + "...[truncated]"
    return value


def compact_state_context(state: Dict[str, Any], *, max_items: int = 25) -> Dict[str, Any]:
    """Provide relevant recent state without injecting unbounded runtime history."""

    domain_state = {key: value for key, value in state.items() if key != "_runtime"}
    return _compact(domain_state, max_items=max_items)


def make_run_id(task_id: str, scheduled_at: str, trigger_slot: str) -> str:
    material = f"{task_id}|{scheduled_at}|{trigger_slot}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def notification_fingerprint(task_id: str, event_key: str) -> str:
    material = f"{task_id}|{event_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def notification_idempotency_key(fingerprint: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"automation-hub:{fingerprint}"))


def build_agent_prompt(
    *,
    task_id: str,
    task_name: str,
    prompt_path: str,
    prompt_text: str,
    state_path: str,
    state_context: Dict[str, Any],
    scheduled_at: str,
    trigger_slot: str,
    timezone_name: str,
    presentation_instruction: str,
    evidence_context: Optional[Dict[str, Any]] = None,
) -> str:
    state_json = json.dumps(state_context, ensure_ascii=False, sort_keys=True)
    evidence_json = json.dumps(
        evidence_context or {}, ensure_ascii=False, sort_keys=True
    )
    return f"""You are executing one Automation Hub production task.

Operational context (this is Harness metadata, not a rewrite of the business prompt):
- task_id: {task_id}
- task_name: {task_name}
- scheduled_at: {scheduled_at}
- trigger_slot: {trigger_slot}
- timezone: {timezone_name}
- prompt_file: {prompt_path}
- full_state_file: {state_path}

Rules:
1. Follow the business prompt below verbatim; do not rewrite or weaken it.
2. Use timezone-aware timestamps in {timezone_name}.
3. Use primary or authoritative sources and record source, data timestamp, and freshness.
4. Treat stale, unavailable, or untrusted data as unavailable. Never replace trusted state with failed/null data.
5. Return only the JSON object required by the supplied output schema.
6. Do not edit TASK.md, state, outputs, logs, or configuration. Do not send Feishu messages. The Harness owns persistence and delivery.
7. state_updates_json must contain only validated domain-state updates as a JSON object string. Use "{{}}" on failure.
8. SUCCESS_NOTIFY requires a stable business event_key that does not depend on prose wording. SUCCESS_NO_NOTIFY means a successful run with no meaningful notification. SKIPPED is a valid no-op such as a non-trading day. FAILED is only a real execution failure.
9. When should_notify is false, notification title/body/event_key must be empty strings and notification.cards must be an empty array.
10. If more history is required, read the full state file; do not copy its complete history into the result.
11. Never read or expose config/.env, credentials, tokens, cookies, or secrets.
12. Notification presentation: {presentation_instruction}
13. Card fields are semantic plain data. Do not return raw Feishu card JSON. The Harness validates, escapes, renders, uploads optional images, and delivers cards.
14. When deterministic evidence is supplied below, treat it as the preferred factual input. Preserve every field's scope, timestamp, freshness, and unavailable marker. Do not replace valid evidence with vague web-search prose, and do not claim the whole market is unverifiable when the evidence contains valid core fields.

Recent relevant state summary:
{state_json}

Deterministic workflow evidence (may be empty for tasks without a collector):
{evidence_json}

Business prompt begins:
---
{prompt_text}
---
Business prompt ends.
"""


def validate_agent_result(value: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(value, dict):
        raise TaskRuntimeError("agent result must be a JSON object")
    required = {
        "status",
        "should_notify",
        "summary",
        "output_markdown",
        "state_updates_json",
        "notification",
        "skip_reason",
        "error",
        "source_metadata",
    }
    missing = sorted(required.difference(value))
    if missing:
        raise TaskRuntimeError("agent result missing fields: " + ", ".join(missing))

    status = value.get("status")
    if status not in RESULT_STATUSES:
        raise TaskRuntimeError("agent result status is invalid")
    should_notify = value.get("should_notify")
    if not isinstance(should_notify, bool):
        raise TaskRuntimeError("agent result should_notify must be boolean")
    if (status == SUCCESS_NOTIFY) != should_notify:
        raise TaskRuntimeError("status and should_notify are inconsistent")
    for key in ("summary", "output_markdown", "state_updates_json", "skip_reason", "error"):
        if not isinstance(value.get(key), str):
            raise TaskRuntimeError(f"agent result {key} must be a string")
    if status in {SUCCESS_NOTIFY, SUCCESS_NO_NOTIFY} and not value["summary"].strip():
        raise TaskRuntimeError("successful agent result requires a summary")
    if status == SKIPPED and not value["skip_reason"].strip():
        raise TaskRuntimeError("skipped agent result requires skip_reason")
    if status == FAILED and not value["error"].strip():
        raise TaskRuntimeError("failed agent result requires error")

    notification = value.get("notification")
    if not isinstance(notification, dict):
        raise TaskRuntimeError("agent result notification must be an object")
    notification_fields = {"title", "body", "event_key", "cards"}
    if set(notification) != notification_fields:
        raise TaskRuntimeError(
            "agent result notification must contain title, body, event_key, and cards"
        )
    for key in ("title", "body", "event_key"):
        if not isinstance(notification.get(key), str):
            raise TaskRuntimeError(f"notification.{key} must be a string")
    if should_notify and any(
        not notification[key].strip() for key in ("title", "body", "event_key")
    ):
        raise TaskRuntimeError("notification fields must be non-empty when notifying")
    try:
        cards = validate_card_specs(notification.get("cards"))
    except CardSpecError as exc:
        raise TaskRuntimeError(str(exc)) from exc
    notification["cards"] = cards
    if not should_notify and (
        any(notification[key] for key in ("title", "body", "event_key")) or cards
    ):
        raise TaskRuntimeError("notification fields must be empty when not notifying")

    try:
        state_updates = json.loads(value["state_updates_json"])
    except json.JSONDecodeError as exc:
        raise TaskRuntimeError("state_updates_json must contain valid JSON") from exc
    if not isinstance(state_updates, dict):
        raise TaskRuntimeError("state_updates_json must contain a JSON object")
    if status == FAILED and state_updates:
        raise TaskRuntimeError("failed runs cannot update domain state")

    metadata = value.get("source_metadata")
    if not isinstance(metadata, list):
        raise TaskRuntimeError("source_metadata must be an array")
    for index, item in enumerate(metadata):
        if not isinstance(item, dict):
            raise TaskRuntimeError(f"source_metadata[{index}] must be an object")
        for key in ("source", "data_timestamp", "freshness"):
            if not isinstance(item.get(key), str):
                raise TaskRuntimeError(
                    f"source_metadata[{index}].{key} must be a string"
                )
    return copy.deepcopy(value), state_updates


def result_markdown(result: Dict[str, Any]) -> str:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    markdown = payload.get("output_markdown", "").strip()
    if markdown:
        return markdown + "\n"
    status = payload.get("status", FAILED)
    detail = payload.get("skip_reason") or payload.get("error") or payload.get("summary")
    return f"# Automation Hub Run\n\n- status: {status}\n- detail: {detail}\n"


def write_run_outputs(
    output_directory: Path,
    *,
    local_date: str,
    timestamp_slug: str,
    run_id: str,
    result: Dict[str, Any],
) -> Tuple[Path, Path]:
    day_directory = output_directory / local_date
    day_directory.mkdir(parents=True, exist_ok=True)
    base = f"{timestamp_slug}-{run_id[:8]}"
    json_path = day_directory / f"{base}.json"
    markdown_path = day_directory / f"{base}.md"
    atomic_write_json(json_path, result)
    atomic_write_text(markdown_path, result_markdown(result))
    return json_path, markdown_path


def prune_runtime(state: Dict[str, Any], *, keep_runs: int = 500, keep_notifications: int = 1000) -> None:
    runtime = state.setdefault("_runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        state["_runtime"] = runtime
    for key, limit in (("processed_runs", keep_runs), ("notifications", keep_notifications)):
        mapping = runtime.get(key)
        if isinstance(mapping, dict) and len(mapping) > limit:
            ordered = sorted(
                mapping.items(),
                key=lambda item: str(
                    item[1].get("updated_at", "")
                    if isinstance(item[1], dict)
                    else ""
                ),
            )
            runtime[key] = dict(ordered[-limit:])


@contextmanager
def run_lock(lock_path: Path, *, stale_seconds: int) -> Iterator[None]:
    """Serialize one task run and recover only explicitly stale lock files."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "created_at": datetime.now().astimezone().isoformat(),
                        },
                        sort_keys=True,
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            acquired = True
            break
        except FileExistsError:
            owner_alive: Optional[bool] = None
            try:
                lock_value = json.loads(lock_path.read_text(encoding="utf-8"))
                owner_pid = lock_value.get("pid") if isinstance(lock_value, dict) else None
                if isinstance(owner_pid, int) and owner_pid > 0:
                    try:
                        os.kill(owner_pid, 0)
                        owner_alive = True
                    except ProcessLookupError:
                        owner_alive = False
                    except PermissionError:
                        owner_alive = True
            except (OSError, json.JSONDecodeError):
                owner_alive = None
            try:
                age_seconds = max(0.0, datetime.now().timestamp() - lock_path.stat().st_mtime)
            except OSError as exc:
                raise TaskRuntimeError(f"cannot inspect task lock: {lock_path}") from exc
            if attempt == 0 and (
                owner_alive is False
                or (owner_alive is None and age_seconds > stale_seconds)
            ):
                lock_path.unlink()
                continue
            raise TaskRuntimeError(f"task is already running: {lock_path.stem}")
    if not acquired:
        raise TaskRuntimeError(f"cannot acquire task lock: {lock_path}")
    try:
        yield
    finally:
        if lock_path.exists():
            lock_path.unlink()
