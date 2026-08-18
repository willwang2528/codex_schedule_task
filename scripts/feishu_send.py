#!/usr/bin/env python3
"""Send text or post messages to a Feishu chat using environment credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
APP_ID_ENV_KEY = "FEISHU_APP_ID_SCHEDULE_TASK"
APP_SECRET_ENV_KEY = "FEISHU_APP_SECRET_SCHEDULE_TASK"
CHAT_ID_ENV_KEY = "FEISHU_CHAT_ID_SCHEDULE_TASK"
FEISHU_ENV_KEYS = (
    APP_ID_ENV_KEY,
    APP_SECRET_ENV_KEY,
    CHAT_ID_ENV_KEY,
)


class FeishuDeliveryError(RuntimeError):
    """A sanitized Feishu API or configuration error."""


def load_local_feishu_env(path: Optional[Path] = None) -> bool:
    """Load namespaced Feishu values from config/.env without overriding exports."""

    env_path = path or Path(__file__).resolve().parents[1] / "config" / ".env"
    if not env_path.is_file():
        return False

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FeishuDeliveryError(
            f"cannot read local Feishu environment file: {env_path}"
        ) from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise FeishuDeliveryError(
                f"invalid config/.env entry at line {line_number}"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in FEISHU_ENV_KEYS:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key not in os.environ and value:
            os.environ[key] = value
    return True


def _request_json(
    url: str,
    payload: Dict[str, Any],
    *,
    bearer_token: Optional[str] = None,
    timeout: int = 15,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FeishuDeliveryError(f"Feishu HTTP error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeishuDeliveryError(f"Feishu network error: {exc.reason}") from exc

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise FeishuDeliveryError("Feishu returned an unexpected response")
    return result


def _get_tenant_access_token(app_id: str, app_secret: str) -> str:
    result = _request_json(AUTH_URL, {"app_id": app_id, "app_secret": app_secret})
    if result.get("code") != 0:
        raise FeishuDeliveryError(
            f"Feishu authentication failed: code={result.get('code')} "
            f"message={result.get('msg', 'unknown')}"
        )
    token = result.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise FeishuDeliveryError("Feishu authentication returned no access token")
    return token


def _post_content(text: str, title: str) -> Dict[str, Any]:
    paragraphs = []
    for line in text.splitlines() or [text]:
        paragraphs.append([{"tag": "text", "text": line or " "}])
    return {"zh_cn": {"title": title, "content": paragraphs}}


def send_message(
    text: str,
    *,
    message_type: str = "text",
    title: str = "Automation Hub",
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not all(os.environ.get(key) for key in FEISHU_ENV_KEYS):
        load_local_feishu_env()
    app_id = os.environ.get(APP_ID_ENV_KEY, "")
    app_secret = os.environ.get(APP_SECRET_ENV_KEY, "")
    resolved_chat_id = chat_id or os.environ.get(CHAT_ID_ENV_KEY, "")
    missing = [
        key
        for key, value in (
            (APP_ID_ENV_KEY, app_id),
            (APP_SECRET_ENV_KEY, app_secret),
            (CHAT_ID_ENV_KEY, resolved_chat_id),
        )
        if not value
    ]
    if missing:
        raise FeishuDeliveryError(
            "missing Feishu environment variables: " + ", ".join(missing)
        )
    if message_type not in {"text", "post"}:
        raise FeishuDeliveryError("message_type must be 'text' or 'post'")

    tenant_token = _get_tenant_access_token(app_id, app_secret)
    content: Dict[str, Any]
    if message_type == "text":
        content = {"text": text}
    else:
        content = _post_content(text, title)

    query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
    result = _request_json(
        f"{MESSAGE_URL}?{query}",
        {
            "receive_id": resolved_chat_id,
            "msg_type": message_type,
            "content": json.dumps(content, ensure_ascii=False),
        },
        bearer_token=tenant_token,
    )
    if result.get("code") != 0:
        raise FeishuDeliveryError(
            f"Feishu delivery failed: code={result.get('code')} "
            f"message={result.get('msg', 'unknown')}"
        )
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {"status": "ok", "message_id": data.get("message_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--content", help="Message content")
    source.add_argument("--file", type=Path, help="UTF-8 text or Markdown file")
    parser.add_argument("--type", choices=("text", "post"), default="text")
    parser.add_argument("--title", default="Automation Hub")
    args = parser.parse_args()

    try:
        text = (
            args.file.read_text(encoding="utf-8")
            if args.file is not None
            else str(args.content)
        )
        result = send_message(text, message_type=args.type, title=args.title)
    except (OSError, FeishuDeliveryError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
