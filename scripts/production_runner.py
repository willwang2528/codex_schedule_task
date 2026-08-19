#!/usr/bin/env python3
"""Execute and finalize structured Codex tasks for Automation Hub."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from feishu_cards import (
    CardSpecError,
    presentation_instruction,
    render_card,
    validate_presentation,
)
from feishu_send import FeishuDeliveryError, send_message, upload_remote_image
from task_runtime import (
    FAILED,
    SKIPPED,
    SUCCESS_NO_NOTIFY,
    SUCCESS_NOTIFY,
    TaskRuntimeError,
    atomic_write_json,
    build_agent_prompt,
    compact_state_context,
    deep_merge,
    make_run_id,
    notification_fingerprint,
    notification_idempotency_key,
    now_in,
    prune_runtime,
    read_json_object,
    run_lock,
    validate_agent_result,
    write_run_outputs,
)


AgentRunner = Callable[[str, Dict[str, Any], Path], Dict[str, Any]]
DeliverySender = Callable[..., Dict[str, Any]]
ImageUploader = Callable[[str], str]
RESERVED_STATE_UPDATE_KEYS = {
    "_runtime",
    "schema_version",
    "task_id",
    "state_version",
    "last_run_at",
    "last_success_at",
}


def _config_int(section: Dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    return int(value) if isinstance(value, (int, float)) else default


def _notification_allowed(delivery_config: Dict[str, Any], trigger_slot: str) -> bool:
    """Return whether this scheduled slot may create or deliver a notification."""

    configured = delivery_config.get("notification_triggers")
    if not isinstance(configured, list):
        return True
    return trigger_slot in {str(value) for value in configured}


def _suppress_result_notification(value: Any) -> Any:
    """Preserve data/state output while enforcing a data-only trigger boundary."""

    if not isinstance(value, dict) or not value.get("should_notify"):
        return value
    suppressed = copy.deepcopy(value)
    suppressed["status"] = SUCCESS_NO_NOTIFY
    suppressed["should_notify"] = False
    suppressed["notification"] = {
        "title": "",
        "body": "",
        "event_key": "",
        "cards": [],
    }
    return suppressed


def _sanitize_error(message: str) -> str:
    sanitized = str(message)
    for key, value in os.environ.items():
        if value and any(
            marker in key.upper()
            for marker in ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY")
        ):
            sanitized = sanitized.replace(value, "[REDACTED]")
    return " ".join(sanitized.split())[:2000]


def _agent_environment() -> Dict[str, str]:
    """Keep runtime essentials while withholding delivery and token credentials."""

    blocked_markers = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in blocked_markers)
        and not key.startswith("FEISHU_")
    }


def _load_result_file(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskRuntimeError(f"cannot read structured result: {path}") from exc
    if not isinstance(value, dict):
        raise TaskRuntimeError("structured result root must be an object")
    return value


def _load_workflow_evidence(
    *,
    config: Dict[str, Any],
    repo_root: Path,
    scheduled_at: str,
    trigger_slot: str,
) -> Dict[str, Any]:
    """Run an optional task-owned deterministic collector before the Agent."""

    workflow = config.get("workflow") if isinstance(config.get("workflow"), dict) else {}
    script_value = workflow.get("context_script")
    if not isinstance(script_value, str) or not script_value:
        return {}
    resolved_root = repo_root.resolve()
    script_path = (resolved_root / script_value).resolve()
    try:
        script_path.relative_to(resolved_root)
    except ValueError as exc:
        raise TaskRuntimeError("workflow.context_script must stay inside the repository") from exc
    if not script_path.is_file():
        raise TaskRuntimeError(f"workflow context script does not exist: {script_value}")

    timeout_value = workflow.get("context_timeout_seconds", 90)
    timeout_seconds = (
        int(timeout_value)
        if isinstance(timeout_value, (int, float)) and not isinstance(timeout_value, bool)
        else 90
    )
    command = [
        sys.executable,
        str(script_path),
        "--scheduled-at",
        scheduled_at,
        "--trigger-slot",
        trigger_slot,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=_agent_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "unavailable",
            "collector": script_value,
            "error": _sanitize_error(str(exc)),
        }
    if completed.returncode != 0:
        return {
            "status": "unavailable",
            "collector": script_value,
            "error": _sanitize_error(
                completed.stderr or completed.stdout or "workflow collector failed"
            ),
        }
    if len(completed.stdout.encode("utf-8")) > 200_000:
        return {
            "status": "unavailable",
            "collector": script_value,
            "error": "workflow collector output exceeded 200000 bytes",
        }
    try:
        evidence = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "status": "unavailable",
            "collector": script_value,
            "error": "workflow collector did not return valid JSON",
        }
    if not isinstance(evidence, dict):
        return {
            "status": "unavailable",
            "collector": script_value,
            "error": "workflow collector JSON root must be an object",
        }
    return evidence


def _codex_agent_runner(prompt: str, config: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    execution = config.get("execution") if isinstance(config.get("execution"), dict) else {}
    timeout_seconds = _config_int(execution, "timeout_seconds", 1800)
    retry_attempts = _config_int(execution, "retry_attempts", 2)
    retry_backoff = float(execution.get("retry_backoff_seconds", 20))
    configured_binary = os.environ.get("AUTOMATION_CODEX_BIN")
    codex_binary = configured_binary or shutil.which("codex")
    if not codex_binary:
        app_binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if app_binary.is_file():
            codex_binary = str(app_binary)
    if not codex_binary:
        raise TaskRuntimeError("Codex CLI executable was not found")

    schema_path = repo_root / "config" / "task-result.schema.json"
    last_error = "agent execution failed"
    for attempt in range(1, retry_attempts + 1):
        descriptor, result_name = tempfile.mkstemp(
            prefix="automation-agent-result-", suffix=".json"
        )
        os.close(descriptor)
        result_path = Path(result_name)
        command = [
            codex_binary,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            str(repo_root),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        model = execution.get("model")
        if isinstance(model, str) and model:
            command.extend(("--model", model))
        reasoning_effort = execution.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            command.extend(("--config", f'model_reasoning_effort="{reasoning_effort}"'))
        command.append("-")
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                input=prompt,
                env=_agent_environment(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode == 0:
                return _load_result_file(result_path)
            last_error = _sanitize_error(
                completed.stderr or completed.stdout or "Codex agent returned non-zero"
            )
        except subprocess.TimeoutExpired:
            last_error = f"Codex agent timed out after {timeout_seconds} seconds"
        except OSError as exc:
            last_error = _sanitize_error(str(exc))
        finally:
            if result_path.exists():
                result_path.unlink()
        if attempt < retry_attempts and retry_backoff > 0:
            time.sleep(retry_backoff * attempt)
    raise TaskRuntimeError(last_error)


def _runtime_maps(state: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    runtime = state.setdefault("_runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        state["_runtime"] = runtime
    processed = runtime.setdefault("processed_runs", {})
    notifications = runtime.setdefault("notifications", {})
    if not isinstance(processed, dict) or not isinstance(notifications, dict):
        raise TaskRuntimeError("state _runtime registry is invalid")
    return processed, notifications


def _append_notification_history(
    state: Dict[str, Any], *, fingerprint: str, event_key: str, run_id: str, sent_at: str
) -> None:
    history = state.get("notification_history")
    if isinstance(history, list):
        history.append(
            {
                "fingerprint": fingerprint,
                "event_key": event_key,
                "run_id": run_id,
                "sent_at": sent_at,
            }
        )
        if len(history) > 1000:
            del history[:-1000]


def _notification_trigger_slot(
    state: Dict[str, Any], notification: Dict[str, Any]
) -> Optional[str]:
    direct = notification.get("trigger_slot")
    if isinstance(direct, str) and direct:
        return direct
    processed, _ = _runtime_maps(state)
    run = processed.get(notification.get("run_id"))
    if isinstance(run, dict):
        value = run.get("trigger_slot")
        if isinstance(value, str) and value:
            return value
    return None


def _deliver_pending(
    *,
    task_id: str,
    state: Dict[str, Any],
    state_path: Path,
    fingerprint: str,
    delivery_config: Dict[str, Any],
    timezone_name: str,
    dry_run_delivery: bool,
    delivery_sender: DeliverySender,
    image_uploader: ImageUploader,
) -> Tuple[str, bool, Optional[str]]:
    _, notifications = _runtime_maps(state)
    notification = notifications.get(fingerprint)
    if not isinstance(notification, dict):
        raise TaskRuntimeError("pending notification record is missing")
    if notification.get("status") == "sent":
        return "duplicate", False, None
    notification_trigger_slot = _notification_trigger_slot(state, notification)
    if isinstance(delivery_config.get("notification_triggers"), list) and (
        not notification_trigger_slot
        or not _notification_allowed(delivery_config, notification_trigger_slot)
    ):
        suppressed_at = now_in(timezone_name).isoformat(timespec="seconds")
        notification["status"] = "suppressed_by_trigger_policy"
        notification["updated_at"] = suppressed_at
        notification["last_error"] = None
        atomic_write_json(state_path, state)
        return "not_allowed_for_trigger", False, None
    if dry_run_delivery:
        return "dry_run", False, None

    messages = notification.get("messages")
    if not isinstance(messages, list):
        messages = [
            {
                "kind": "post",
                "status": "pending",
                "title": str(notification.get("title", "Automation Hub")),
                "body": str(notification.get("body", "")),
                "idempotency_key": str(
                    notification.get("idempotency_key")
                    or notification_idempotency_key(fingerprint)
                ),
            }
        ]
        notification["messages"] = messages

    retry_attempts = _config_int(delivery_config, "retry_attempts", 2)
    route_kwargs: Dict[str, str] = {}
    chat_id_env = delivery_config.get("chat_id_env")
    if isinstance(chat_id_env, str) and chat_id_env:
        route_kwargs["chat_id_env"] = chat_id_env
    last_error: Optional[str] = None
    for message in messages:
        if not isinstance(message, dict):
            raise TaskRuntimeError("pending notification message is invalid")
        if message.get("status") == "sent":
            continue
        for attempt in range(1, retry_attempts + 1):
            try:
                kind = message.get("kind")
                if kind == "interactive":
                    card_spec = message.get("card")
                    if not isinstance(card_spec, dict):
                        raise TaskRuntimeError("pending card specification is invalid")
                    image_url = card_spec.get("image_url")
                    if (
                        isinstance(image_url, str)
                        and image_url
                        and not message.get("image_key")
                        and message.get("image_status") != "skipped"
                    ):
                        try:
                            message["image_key"] = image_uploader(image_url)
                            message["image_status"] = "uploaded"
                        except (FeishuDeliveryError, OSError) as image_exc:
                            message["image_status"] = "skipped"
                            message["image_error"] = _sanitize_error(str(image_exc))
                        atomic_write_json(state_path, state)
                    try:
                        card_payload = render_card(
                            card_spec,
                            image_key=str(message.get("image_key") or ""),
                        )
                    except CardSpecError as exc:
                        raise TaskRuntimeError(str(exc)) from exc
                    response = delivery_sender(
                        "",
                        message_type="interactive",
                        card=card_payload,
                        idempotency_key=str(message["idempotency_key"]),
                        **route_kwargs,
                    )
                elif kind == "post":
                    response = delivery_sender(
                        str(message.get("body", notification.get("body", ""))),
                        message_type="post",
                        title=str(
                            message.get("title", notification.get("title", ""))
                        ),
                        idempotency_key=str(message["idempotency_key"]),
                        **route_kwargs,
                    )
                else:
                    raise TaskRuntimeError("pending notification kind is invalid")
                if not isinstance(response, dict):
                    raise TaskRuntimeError("delivery adapter returned an invalid response")
                message_sent_at = now_in(timezone_name).isoformat(timespec="seconds")
                message["status"] = "sent"
                message["sent_at"] = message_sent_at
                message["message_id"] = response.get("message_id")
                message["last_error"] = None
                notification["updated_at"] = message_sent_at
                notification["last_error"] = None
                atomic_write_json(state_path, state)
                break
            except (FeishuDeliveryError, OSError, TaskRuntimeError) as exc:
                last_error = _sanitize_error(str(exc))
                message["last_error"] = last_error
                notification["last_error"] = last_error
                notification["updated_at"] = now_in(timezone_name).isoformat(
                    timespec="seconds"
                )
                atomic_write_json(state_path, state)
                if attempt < retry_attempts:
                    time.sleep(min(30, 2 ** (attempt - 1)))
        if message.get("status") != "sent":
            return "failed", False, last_error

    sent_at = now_in(timezone_name).isoformat(timespec="seconds")
    notification["status"] = "sent"
    notification["sent_at"] = sent_at
    notification["updated_at"] = sent_at
    notification["message_ids"] = [
        message.get("message_id") for message in messages if message.get("message_id")
    ]
    notification["last_error"] = None
    _append_notification_history(
        state,
        fingerprint=fingerprint,
        event_key=str(notification["event_key"]),
        run_id=str(notification["run_id"]),
        sent_at=sent_at,
    )
    prune_runtime(state)
    atomic_write_json(state_path, state)
    return "ok", True, None


def recover_pending_delivery(
    *,
    config: Dict[str, Any],
    state_path: Path,
    dry_run_delivery: bool = False,
    delivery_sender: DeliverySender = send_message,
    image_uploader: ImageUploader = upload_remote_image,
) -> Dict[str, Any]:
    state = read_json_object(state_path)
    _, notifications = _runtime_maps(state)
    delivery_config = config.get("delivery") if isinstance(config.get("delivery"), dict) else {}
    if delivery_config.get("type") != "feishu" or not delivery_config.get("enabled"):
        return {"status": SUCCESS_NO_NOTIFY, "recovered": 0, "failed": 0}
    recovered = 0
    failed = 0
    errors = []
    for fingerprint, notification in list(notifications.items()):
        if not isinstance(notification, dict) or notification.get("status") != "pending":
            continue
        delivery_status, sent, error = _deliver_pending(
            task_id=str(config["id"]),
            state=state,
            state_path=state_path,
            fingerprint=fingerprint,
            delivery_config=delivery_config,
            timezone_name=str(config["schedule"]["timezone"]),
            dry_run_delivery=dry_run_delivery,
            delivery_sender=delivery_sender,
            image_uploader=image_uploader,
        )
        if sent or delivery_status == "dry_run":
            recovered += 1
        elif delivery_status == "not_allowed_for_trigger":
            continue
        else:
            failed += 1
            if error:
                errors.append(error)
    return {
        "status": FAILED if failed else SUCCESS_NO_NOTIFY,
        "recovered": recovered,
        "failed": failed,
        "error": "; ".join(errors)[:2000] or None,
    }


def execute_production_task(
    *,
    repo_root: Path,
    config: Dict[str, Any],
    prompt_path: Path,
    state_path: Path,
    output_directory: Path,
    scheduled_at: datetime,
    trigger_slot: str,
    result_file: Optional[Path] = None,
    dry_run_delivery: bool = False,
    force: bool = False,
    agent_runner: Optional[AgentRunner] = None,
    delivery_sender: DeliverySender = send_message,
    image_uploader: ImageUploader = upload_remote_image,
) -> Dict[str, Any]:
    task_id = str(config["id"])
    task_name = str(config["name"])
    timezone_name = str(config["schedule"]["timezone"])
    scheduled_at = scheduled_at.astimezone(now_in(timezone_name).tzinfo)
    scheduled_at_text = scheduled_at.isoformat(timespec="seconds")
    run_id = make_run_id(task_id, scheduled_at_text, trigger_slot)
    execution = config.get("execution") if isinstance(config.get("execution"), dict) else {}
    timeout_seconds = _config_int(execution, "timeout_seconds", 1800)
    execution_attempts = _config_int(execution, "retry_attempts", 2)
    retry_backoff = float(execution.get("retry_backoff_seconds", 20))
    lock_stale_seconds = int(
        timeout_seconds * execution_attempts
        + retry_backoff * max(0, execution_attempts - 1) * execution_attempts / 2
        + 300
    )
    lock_path = repo_root / "state" / ".locks" / f"{task_id}.lock"
    delivery_config = (
        config.get("delivery") if isinstance(config.get("delivery"), dict) else {}
    )
    presentation = str(delivery_config.get("presentation", "post"))
    notification_allowed = _notification_allowed(delivery_config, trigger_slot)
    try:
        agent_presentation_instruction = presentation_instruction(presentation)
    except CardSpecError as exc:
        raise TaskRuntimeError(str(exc)) from exc
    if notification_allowed:
        configured_notification_triggers = delivery_config.get("notification_triggers")
        if isinstance(configured_notification_triggers, list):
            agent_presentation_instruction += (
                f" This trigger ({trigger_slot}) is notification-enabled. A successful "
                "normal run must return SUCCESS_NOTIFY; SKIPPED and FAILED remain valid "
                "for genuine no-op or failure conditions."
            )
    else:
        agent_presentation_instruction = (
            f"This trigger ({trigger_slot}) is data-only. Return SUCCESS_NO_NOTIFY with "
            "empty notification fields and cards while preserving the factual summary, "
            "local output, and safe state updates. The Harness will suppress any attempted "
            "notification for this slot."
        )

    with run_lock(lock_path, stale_seconds=lock_stale_seconds):
        state = read_json_object(state_path)
        processed, notifications = _runtime_maps(state)
        prior = processed.get(run_id)
        if isinstance(prior, dict) and prior.get("terminal") and not force:
            fingerprint = prior.get("notification_fingerprint")
            if isinstance(fingerprint, str):
                pending = notifications.get(fingerprint)
                if isinstance(pending, dict) and pending.get("status") == "pending":
                    delivery_status, sent, error = _deliver_pending(
                        task_id=task_id,
                        state=state,
                        state_path=state_path,
                        fingerprint=fingerprint,
                        delivery_config=dict(config["delivery"]),
                        timezone_name=timezone_name,
                        dry_run_delivery=dry_run_delivery,
                        delivery_sender=delivery_sender,
                        image_uploader=image_uploader,
                    )
                    duplicate_status = (
                        SUCCESS_NO_NOTIFY
                        if delivery_status == "not_allowed_for_trigger"
                        else SUCCESS_NOTIFY if sent else FAILED
                    )
                    return {
                        "task": task_id,
                        "run_id": run_id,
                        "status": duplicate_status,
                        "duplicate_run": True,
                        "notification_sent": sent,
                        "delivery_status": delivery_status,
                        "error": error,
                    }
            return {
                "task": task_id,
                "run_id": run_id,
                "status": str(prior.get("status", SUCCESS_NO_NOTIFY)),
                "duplicate_run": True,
                "notification_sent": False,
                "delivery_status": "duplicate",
                "error": None,
            }

        started_at = now_in(timezone_name).isoformat(timespec="seconds")
        processed[run_id] = {
            "status": "RUNNING",
            "terminal": False,
            "scheduled_at": scheduled_at_text,
            "trigger_slot": trigger_slot,
            "started_at": started_at,
            "updated_at": started_at,
        }
        prune_runtime(state)
        atomic_write_json(state_path, state)

        prompt_text = prompt_path.read_text(encoding="utf-8")
        workflow_evidence = _load_workflow_evidence(
            config=config,
            repo_root=repo_root,
            scheduled_at=scheduled_at_text,
            trigger_slot=trigger_slot,
        )
        agent_prompt = build_agent_prompt(
            task_id=task_id,
            task_name=task_name,
            prompt_path=str(prompt_path.relative_to(repo_root)),
            prompt_text=prompt_text,
            state_path=str(state_path.relative_to(repo_root)),
            state_context=compact_state_context(
                state,
                max_items=_config_int(config.get("state", {}), "context_recent_items", 25),
            ),
            scheduled_at=scheduled_at_text,
            trigger_slot=trigger_slot,
            timezone_name=timezone_name,
            presentation_instruction=agent_presentation_instruction,
            evidence_context=workflow_evidence,
        )

        try:
            validation_attempts = 1 if result_file is not None else 2
            current_prompt = agent_prompt
            for validation_attempt in range(1, validation_attempts + 1):
                raw_result = (
                    _load_result_file(result_file)
                    if result_file is not None
                    else (agent_runner or _codex_agent_runner)(
                        current_prompt, config, repo_root
                    )
                )
                if not notification_allowed:
                    raw_result = _suppress_result_notification(raw_result)
                try:
                    result, state_updates = validate_agent_result(raw_result)
                    if (
                        notification_allowed
                        and isinstance(
                            delivery_config.get("notification_triggers"), list
                        )
                        and result["status"] == SUCCESS_NO_NOTIFY
                    ):
                        raise TaskRuntimeError(
                            f"trigger {trigger_slot} requires SUCCESS_NOTIFY on a successful run"
                        )
                    if (
                        delivery_config.get("policy") == "always"
                        and result["status"] == SUCCESS_NO_NOTIFY
                    ):
                        raise TaskRuntimeError(
                            "delivery.policy=always requires SUCCESS_NOTIFY for a successful run"
                        )
                    validate_presentation(
                        presentation,
                        result["notification"]["cards"],
                        should_notify=bool(result["should_notify"]),
                    )
                    reserved = sorted(
                        RESERVED_STATE_UPDATE_KEYS.intersection(state_updates)
                    )
                    if reserved:
                        raise TaskRuntimeError(
                            "state updates contain Harness-owned keys: "
                            + ", ".join(reserved)
                        )
                    break
                except (CardSpecError, TaskRuntimeError) as validation_exc:
                    if validation_attempt >= validation_attempts:
                        raise TaskRuntimeError(str(validation_exc)) from validation_exc
                    current_prompt = (
                        agent_prompt
                        + "\n\nThe previous structured result was rejected by the Harness: "
                        + _sanitize_error(str(validation_exc))
                        + "\nReturn a corrected complete JSON object. Do not reuse the invalid shape."
                    )
        except (OSError, TaskRuntimeError) as exc:
            result = {
                "status": FAILED,
                "should_notify": False,
                "summary": "",
                "output_markdown": "",
                "state_updates_json": "{}",
                "notification": {
                    "title": "",
                    "body": "",
                    "event_key": "",
                    "cards": [],
                },
                "skip_reason": "",
                "error": _sanitize_error(str(exc)),
                "source_metadata": [],
            }
            state_updates = {}

        finished_at = now_in(timezone_name)
        finished_at_text = finished_at.isoformat(timespec="seconds")
        envelope = {
            "task_id": task_id,
            "run_id": run_id,
            "scheduled_at": scheduled_at_text,
            "started_at": started_at,
            "finished_at": finished_at_text,
            "trigger_slot": trigger_slot,
            "result": result,
        }
        timestamp_slug = finished_at.strftime("%Y%m%dT%H%M%S%z")
        json_path, markdown_path = write_run_outputs(
            output_directory,
            local_date=finished_at.date().isoformat(),
            timestamp_slug=timestamp_slug,
            run_id=run_id,
            result=envelope,
        )

        status = str(result["status"])
        notification_sent = False
        delivery_status = (
            "not_allowed_for_trigger" if not notification_allowed else "not_requested"
        )
        delivery_error: Optional[str] = None
        fingerprint: Optional[str] = None

        if status != FAILED:
            new_state = deep_merge(state, state_updates)
            new_state["last_run_at"] = finished_at_text
            if status in {SUCCESS_NOTIFY, SUCCESS_NO_NOTIFY}:
                new_state["last_success_at"] = finished_at_text
            new_state["state_version"] = int(new_state.get("state_version", 0)) + 1
            if bool(config.get("state", {}).get("track_trigger_slots")):
                slots = new_state.setdefault("completed_trigger_slots", [])
                slot_key = f"{scheduled_at.date().isoformat()}|{trigger_slot}"
                if isinstance(slots, list) and slot_key not in slots:
                    slots.append(slot_key)
            processed, notifications = _runtime_maps(new_state)

            if result["should_notify"]:
                event_key = str(result["notification"]["event_key"])
                fingerprint = notification_fingerprint(task_id, event_key)
                existing_notification = notifications.get(fingerprint)
                if isinstance(existing_notification, dict) and existing_notification.get("status") == "sent":
                    status = SUCCESS_NO_NOTIFY
                    delivery_status = "duplicate_suppressed"
                elif (
                    isinstance(existing_notification, dict)
                    and existing_notification.get("status") == "pending"
                ):
                    delivery_status = "pending_reused"
                else:
                    card_specs = result["notification"]["cards"]
                    messages = (
                        [
                            {
                                "kind": "interactive",
                                "status": "pending",
                                "card": card_spec,
                                "idempotency_key": notification_idempotency_key(
                                    f"{fingerprint}:card:{index}"
                                ),
                                "image_status": "pending"
                                if card_spec.get("image_url")
                                else "none",
                            }
                            for index, card_spec in enumerate(card_specs)
                        ]
                        if card_specs
                        else [
                            {
                                "kind": "post",
                                "status": "pending",
                                "title": str(result["notification"]["title"]),
                                "body": str(result["notification"]["body"]),
                                "idempotency_key": notification_idempotency_key(
                                    f"{fingerprint}:post:0"
                                ),
                            }
                        ]
                    )
                    notifications[fingerprint] = {
                        "status": "pending",
                        "run_id": run_id,
                        "trigger_slot": trigger_slot,
                        "event_key": event_key,
                        "title": str(result["notification"]["title"]),
                        "body": str(result["notification"]["body"]),
                        "idempotency_key": notification_idempotency_key(fingerprint),
                        "presentation": presentation,
                        "messages": messages,
                        "created_at": finished_at_text,
                        "updated_at": finished_at_text,
                        "last_error": None,
                    }

            processed[run_id] = {
                "status": status,
                "terminal": True,
                "scheduled_at": scheduled_at_text,
                "trigger_slot": trigger_slot,
                "started_at": started_at,
                "finished_at": finished_at_text,
                "updated_at": finished_at_text,
                "output_json": str(json_path.relative_to(repo_root)),
                "output_markdown": str(markdown_path.relative_to(repo_root)),
                "notification_fingerprint": fingerprint,
            }
            prune_runtime(new_state)
            atomic_write_json(state_path, new_state)
            state = new_state

            if fingerprint and delivery_status != "duplicate_suppressed":
                delivery_config = dict(config["delivery"])
                if (
                    delivery_config.get("enabled")
                    and delivery_config.get("type") == "feishu"
                    and delivery_config.get("policy", "conditional") != "never"
                ):
                    delivery_status, notification_sent, delivery_error = _deliver_pending(
                        task_id=task_id,
                        state=state,
                        state_path=state_path,
                        fingerprint=fingerprint,
                        delivery_config=delivery_config,
                        timezone_name=timezone_name,
                        dry_run_delivery=dry_run_delivery,
                        delivery_sender=delivery_sender,
                        image_uploader=image_uploader,
                    )
                    if delivery_status == "failed":
                        status = FAILED
                else:
                    delivery_status = "skipped"
        else:
            processed, _ = _runtime_maps(state)
            processed[run_id] = {
                "status": FAILED,
                "terminal": True,
                "scheduled_at": scheduled_at_text,
                "trigger_slot": trigger_slot,
                "started_at": started_at,
                "finished_at": finished_at_text,
                "updated_at": finished_at_text,
                "output_json": str(json_path.relative_to(repo_root)),
                "output_markdown": str(markdown_path.relative_to(repo_root)),
                "error": str(result["error"]),
            }
            prune_runtime(state)
            atomic_write_json(state_path, state)
            delivery_error = str(result["error"])

        envelope["final"] = {
            "status": status,
            "state_version": state.get("state_version", 0),
            "notification_sent": notification_sent,
            "delivery_status": delivery_status,
            "error": delivery_error,
        }
        atomic_write_json(json_path, envelope)
        return {
            "task": task_id,
            "run_id": run_id,
            "scheduled_at": scheduled_at_text,
            "trigger_slot": trigger_slot,
            "status": status,
            "state_version": state.get("state_version", 0),
            "notification_sent": notification_sent,
            "delivery_status": delivery_status,
            "output_json": str(json_path.relative_to(repo_root)),
            "output_markdown": str(markdown_path.relative_to(repo_root)),
            "error": delivery_error,
        }
