#!/usr/bin/env python3
"""Render, send, and read back one semantic Feishu Card 2.0 specification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from feishu_cards import (
    CardSpecError,
    render_card,
    validate_card_specs,
    validate_presentation,
)
from feishu_send import CHAT_ID_ENV_KEY, FeishuDeliveryError, read_message, send_message
from task_runtime import TaskRuntimeError, atomic_write_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="UTF-8 semantic card JSON file")
    source.add_argument(
        "--message-id",
        help="Read back a previously accepted message without sending it again",
    )
    parser.add_argument(
        "--presentation",
        default="repo_digest_card",
        help="Semantic presentation profile (default: repo_digest_card)",
    )
    parser.add_argument(
        "--idempotency-key",
        help="Stable Feishu UUID; required when --file is used",
    )
    parser.add_argument(
        "--receipt-file",
        type=Path,
        help="Atomic delivery receipt; required when --file is used",
    )
    parser.add_argument(
        "--chat-id-env",
        help="Optional allowed task-specific FEISHU_CHAT_ID_*_SCHEDULE_TASK variable",
    )
    return parser


def _print_result(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _digest(value: Dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_receipt(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CardSpecError("delivery receipt must contain one JSON object")
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    route_kwargs = {"chat_id_env": args.chat_id_env} if args.chat_id_env else {}
    route_env_key = args.chat_id_env or CHAT_ID_ENV_KEY
    message_id = str(args.message_id or "").strip()
    receipt: Optional[Dict[str, Any]] = None
    receipt_path: Optional[Path] = args.receipt_file
    stage = "readback" if message_id else "validation"

    try:
        if args.file is not None:
            if not args.idempotency_key:
                raise CardSpecError("--idempotency-key is required with --file")
            if receipt_path is None:
                raise CardSpecError("--receipt-file is required with --file")

            raw = json.loads(args.file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise CardSpecError("semantic card file must contain one JSON object")
            card_spec = validate_card_specs([raw])[0]
            validate_presentation(args.presentation, [card_spec], should_notify=True)
            card_digest = _digest(card_spec)
            receipt = _load_receipt(receipt_path)

            if receipt is not None:
                if receipt.get("idempotency_key") != args.idempotency_key:
                    raise CardSpecError(
                        "delivery receipt idempotency key does not match this run"
                    )
                if receipt.get("card_digest") != card_digest:
                    raise CardSpecError(
                        "delivery receipt card digest does not match this run"
                    )
                if receipt.get("chat_id_env") != route_env_key:
                    raise CardSpecError(
                        "delivery receipt chat route does not match this run"
                    )
                message_id = str(receipt.get("message_id") or "").strip()
                if not message_id:
                    raise CardSpecError(
                        "delivery receipt is missing a non-empty message_id"
                    )
            else:
                card = render_card(card_spec)
                stage = "send"
                response = send_message(
                    "",
                    message_type="interactive",
                    card=card,
                    idempotency_key=args.idempotency_key,
                    **route_kwargs,
                )
                message_id = str(response.get("message_id") or "").strip()
                if not message_id:
                    raise FeishuDeliveryError(
                        "Feishu delivery returned no non-empty message_id"
                    )
                receipt = {
                    "version": 1,
                    "status": "accepted",
                    "presentation": args.presentation,
                    "idempotency_key": args.idempotency_key,
                    "card_digest": card_digest,
                    "chat_id_env": route_env_key,
                    "message_id": message_id,
                    "accepted_at": _timestamp(),
                }
                atomic_write_json(receipt_path, receipt)

        if not message_id:
            raise CardSpecError("--message-id must not be empty")
        stage = "readback"
        readback = read_message(message_id, **route_kwargs)
        if readback.get("message_id") != message_id:
            raise FeishuDeliveryError(
                "Feishu readback returned a different message_id"
            )
        if readback.get("msg_type") != "interactive":
            raise FeishuDeliveryError(
                "Feishu readback message is not an interactive card"
            )

        if receipt is not None and receipt_path is not None:
            receipt["status"] = "verified"
            receipt["verified_at"] = _timestamp()
            receipt["msg_type"] = "interactive"
            atomic_write_json(receipt_path, receipt)
    except (
        CardSpecError,
        FeishuDeliveryError,
        TaskRuntimeError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        failure: Dict[str, Any] = {
            "status": "failed",
            "stage": stage,
            "error": str(exc),
        }
        if message_id:
            failure["message_id"] = message_id
        _print_result(failure)
        return 1

    _print_result(
        {
            "status": "ok",
            "message_id": message_id,
            "msg_type": "interactive",
            "readback_status": "verified",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
