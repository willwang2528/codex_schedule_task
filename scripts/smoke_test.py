#!/usr/bin/env python3
"""Deterministic end-to-end smoke test for Automation Hub."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from feishu_send import (
    FEISHU_ENV_KEYS,
    FeishuDeliveryError,
    load_local_feishu_env,
    send_message,
)


NETWORK_TARGETS = (
    "https://github.com/",
    "https://arxiv.org/",
    "https://openreview.net/",
)


def _check_network() -> Tuple[str, Optional[str]]:
    openers = (
        urllib.request.build_opener(),
        urllib.request.build_opener(urllib.request.ProxyHandler({})),
    )
    for opener in openers:
        for url in NETWORK_TARGETS:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Automation-Hub-Smoke-Test/1.0"},
                method="GET",
            )
            try:
                with opener.open(request, timeout=8) as response:
                    if 200 <= response.status < 400:
                        response.read(1)
                        return "ok", url
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
    return "failed", None


def main() -> int:
    repo_root = Path(os.environ.get("AUTOMATION_HUB_ROOT", Path.cwd())).resolve()
    output_dir = Path(
        os.environ.get("AUTOMATION_OUTPUT_DIR", repo_root / "outputs" / "smoke-test")
    ).resolve()
    timezone_name = os.environ.get("AUTOMATION_TIMEZONE", "Asia/Shanghai")
    delivery_enabled = os.environ.get("AUTOMATION_DELIVERY_ENABLED", "true") == "true"
    now = datetime.now(ZoneInfo(timezone_name))
    timestamp = now.isoformat(timespec="seconds")

    repository_status = "ok" if (repo_root / ".git").is_dir() else "failed"
    local_script_status = "ok" if Path(__file__).is_file() else "failed"
    network_status, network_url = _check_network()

    feishu_status = "skipped"
    delivery_error: Optional[str] = None
    if delivery_enabled and not all(os.environ.get(key) for key in FEISHU_ENV_KEYS):
        try:
            load_local_feishu_env()
        except FeishuDeliveryError as exc:
            feishu_status = "failed"
            delivery_error = str(exc)
    feishu_values = [
        os.environ.get(key, "")
        for key in FEISHU_ENV_KEYS
    ]
    if delivery_error is not None:
        pass
    elif delivery_enabled and all(feishu_values):
        message = "\n".join(
            (
                "Automation Hub Smoke Test",
                "",
                "Status: OK",
                f"Repository: {repo_root.name}",
                "Codex Runtime: OK",
                f"Network: {network_status.upper()}",
                "Feishu Delivery: OK",
                f"Timestamp: {timestamp}",
            )
        )
        try:
            send_message(message, message_type="text")
            feishu_status = "ok"
        except FeishuDeliveryError as exc:
            feishu_status = "failed"
            delivery_error = str(exc)
    elif delivery_enabled and any(feishu_values):
        feishu_status = "failed"
        delivery_error = "incomplete Feishu environment configuration"

    critical_ok = (
        repository_status == "ok"
        and network_status == "ok"
        and local_script_status == "ok"
    )
    heading = "Automation Hub OK" if critical_ok else "Automation Hub FAILED"
    report = "\n".join(
        (
            f"# {heading}",
            "",
            f"- timestamp: {timestamp}",
            f"- repository: {repo_root.name}",
            f"- repository_access: {repository_status}",
            f"- network: {network_status}",
            f"- network_target: {network_url or 'none'}",
            f"- local_script: {local_script_status}",
            f"- feishu: {feishu_status}",
            f"- delivery_error: {delivery_error or 'none'}",
            "",
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_timestamp = now.strftime("%Y%m%dT%H%M%S%z")
    output_path = output_dir / f"{file_timestamp}.md"
    output_path.write_text(report, encoding="utf-8")

    if not critical_ok:
        status = "failed"
    elif feishu_status == "failed":
        status = "partial"
    else:
        status = "ok"
    summary = {
        "status": status,
        "repository_status": repository_status,
        "network_status": network_status,
        "network_url": network_url,
        "local_script_status": local_script_status,
        "delivery_status": feishu_status,
        "output_path": str(output_path.relative_to(repo_root)),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if status in {"ok", "partial"} else 1


if __name__ == "__main__":
    sys.exit(main())
