#!/usr/bin/env python3
"""Send text or post messages to a Feishu chat using environment credentials."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
MAX_REMOTE_IMAGE_BYTES = 10 * 1024 * 1024
APP_ID_ENV_KEY = "FEISHU_APP_ID_SCHEDULE_TASK"
APP_SECRET_ENV_KEY = "FEISHU_APP_SECRET_SCHEDULE_TASK"
CHAT_ID_ENV_KEY = "FEISHU_CHAT_ID_SCHEDULE_TASK"
FEISHU_ENV_KEYS = (
    APP_ID_ENV_KEY,
    APP_SECRET_ENV_KEY,
    CHAT_ID_ENV_KEY,
)
TASK_CHAT_ID_ENV_PATTERN = re.compile(
    r"^FEISHU_CHAT_ID_[A-Z0-9_]+_SCHEDULE_TASK$"
)


def _is_local_feishu_env_key(key: str) -> bool:
    return key in FEISHU_ENV_KEYS or bool(TASK_CHAT_ID_ENV_PATTERN.fullmatch(key))


class FeishuDeliveryError(RuntimeError):
    """A sanitized Feishu API or configuration error."""


def _safe_api_message(value: Any) -> str:
    message = re.sub(r"\s+", " ", str(value or "")).strip()
    for key, secret_value in os.environ.items():
        if _is_local_feishu_env_key(key) and secret_value:
            message = message.replace(secret_value, "[REDACTED]")
    return message[:300]


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
        if not _is_local_feishu_env_key(key):
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
        detail = ""
        try:
            error_value = json.loads(exc.read().decode("utf-8", errors="replace"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            error_value = None
        if isinstance(error_value, dict):
            code = error_value.get("code")
            message = error_value.get("msg") or error_value.get("message")
            safe_message = _safe_api_message(message)
            if code is not None:
                detail += f" code={code}"
            if safe_message:
                detail += f" message={safe_message}"
        raise FeishuDeliveryError(
            f"Feishu HTTP error: status={exc.code}{detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FeishuDeliveryError(f"Feishu network error: {exc.reason}") from exc

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise FeishuDeliveryError("Feishu returned an unexpected response")
    return result


def _assert_public_https_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise FeishuDeliveryError("card image URL must be a public HTTPS URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FeishuDeliveryError("card image host could not be resolved") from exc
    for address in addresses:
        try:
            candidate = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise FeishuDeliveryError("card image host resolved unexpectedly") from exc
        if not candidate.is_global:
            raise FeishuDeliveryError("card image URL cannot target a private host")


class _PublicImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        _assert_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_remote_image(url: str, *, timeout: int = 15) -> tuple[bytes, str, str]:
    _assert_public_https_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "image/*",
            "User-Agent": "Automation-Hub-Card-Image/1.0",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_PublicImageRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            _assert_public_https_url(final_url)
            content_type = response.headers.get_content_type().lower()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_REMOTE_IMAGE_BYTES:
                raise FeishuDeliveryError("card image exceeds the 10 MB limit")
            data = response.read(MAX_REMOTE_IMAGE_BYTES + 1)
    except FeishuDeliveryError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise FeishuDeliveryError(f"card image download failed: {exc}") from exc
    if len(data) > MAX_REMOTE_IMAGE_BYTES:
        raise FeishuDeliveryError("card image exceeds the 10 MB limit")
    if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        raise FeishuDeliveryError("card image has an unsupported content type")
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[content_type]
    return data, content_type, f"card-image{extension}"


def _upload_image_bytes(
    data: bytes, *, content_type: str, filename: str, bearer_token: str, timeout: int = 15
) -> str:
    boundary = f"automation-hub-{uuid.uuid4().hex}"
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image_type"\r\n\r\n',
            b"message\r\n",
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="image"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    request = urllib.request.Request(
        IMAGE_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_response = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FeishuDeliveryError(
            f"Feishu image upload HTTP error: status={exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FeishuDeliveryError(f"Feishu image upload network error: {exc.reason}") from exc
    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise FeishuDeliveryError("Feishu image upload returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("code") != 0:
        code = result.get("code") if isinstance(result, dict) else "unknown"
        message = result.get("msg") if isinstance(result, dict) else "unknown"
        raise FeishuDeliveryError(
            f"Feishu image upload failed: code={code} message={_safe_api_message(message)}"
        )
    data_value = result.get("data")
    image_key = data_value.get("image_key") if isinstance(data_value, dict) else None
    if not isinstance(image_key, str) or not image_key:
        raise FeishuDeliveryError("Feishu image upload returned no image key")
    return image_key


def upload_remote_image(url: str) -> str:
    """Download a public HTTPS image and upload it as a Feishu message resource."""

    if not all(os.environ.get(key) for key in (APP_ID_ENV_KEY, APP_SECRET_ENV_KEY)):
        load_local_feishu_env()
    app_id = os.environ.get(APP_ID_ENV_KEY, "")
    app_secret = os.environ.get(APP_SECRET_ENV_KEY, "")
    if not app_id or not app_secret:
        raise FeishuDeliveryError("missing Feishu credentials for card image upload")
    image, content_type, filename = _download_remote_image(url)
    tenant_token = _get_tenant_access_token(app_id, app_secret)
    return _upload_image_bytes(
        image,
        content_type=content_type,
        filename=filename,
        bearer_token=tenant_token,
    )


def _get_tenant_access_token(app_id: str, app_secret: str) -> str:
    result = _request_json(AUTH_URL, {"app_id": app_id, "app_secret": app_secret})
    if result.get("code") != 0:
        raise FeishuDeliveryError(
            f"Feishu authentication failed: code={result.get('code')} "
            f"message={_safe_api_message(result.get('msg', 'unknown'))}"
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
    chat_id_env: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    card: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    route_env_key = chat_id_env or CHAT_ID_ENV_KEY
    if chat_id_env and not TASK_CHAT_ID_ENV_PATTERN.fullmatch(chat_id_env):
        raise FeishuDeliveryError("chat_id_env is not an allowed task chat variable")
    if not (
        os.environ.get(APP_ID_ENV_KEY)
        and os.environ.get(APP_SECRET_ENV_KEY)
        and (chat_id or os.environ.get(route_env_key))
    ):
        load_local_feishu_env()
    app_id = os.environ.get(APP_ID_ENV_KEY, "")
    app_secret = os.environ.get(APP_SECRET_ENV_KEY, "")
    resolved_chat_id = chat_id or os.environ.get(route_env_key, "")
    missing = [
        key
        for key, value in (
            (APP_ID_ENV_KEY, app_id),
            (APP_SECRET_ENV_KEY, app_secret),
            (route_env_key, resolved_chat_id),
        )
        if not value
    ]
    if missing:
        raise FeishuDeliveryError(
            "missing Feishu environment variables: " + ", ".join(missing)
        )
    if message_type not in {"text", "post", "interactive"}:
        raise FeishuDeliveryError(
            "message_type must be 'text', 'post', or 'interactive'"
        )
    if message_type == "interactive" and not isinstance(card, dict):
        raise FeishuDeliveryError("interactive messages require a card object")
    if message_type != "interactive" and card is not None:
        raise FeishuDeliveryError("card is only valid for interactive messages")

    tenant_token = _get_tenant_access_token(app_id, app_secret)
    content: Dict[str, Any]
    if message_type == "text":
        content = {"text": text}
    elif message_type == "post":
        content = _post_content(text, title)
    else:
        content = card or {}

    query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
    message_payload: Dict[str, Any] = {
        "receive_id": resolved_chat_id,
        "msg_type": message_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    if idempotency_key:
        if len(idempotency_key) > 50:
            raise FeishuDeliveryError("idempotency_key must be 50 characters or fewer")
        message_payload["uuid"] = idempotency_key

    result = _request_json(
        f"{MESSAGE_URL}?{query}",
        message_payload,
        bearer_token=tenant_token,
    )
    if result.get("code") != 0:
        raise FeishuDeliveryError(
            f"Feishu delivery failed: code={result.get('code')} "
            f"message={_safe_api_message(result.get('msg', 'unknown'))}"
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
    parser.add_argument(
        "--idempotency-key",
        help="Stable key forwarded to Feishu to suppress duplicate sends",
    )
    args = parser.parse_args()

    try:
        text = (
            args.file.read_text(encoding="utf-8")
            if args.file is not None
            else str(args.content)
        )
        result = send_message(
            text,
            message_type=args.type,
            title=args.title,
            idempotency_key=args.idempotency_key,
        )
    except (OSError, FeishuDeliveryError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
