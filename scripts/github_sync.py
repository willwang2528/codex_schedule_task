#!/usr/bin/env python3
"""Publish finalized Automation Hub research cards to the configured Git remote."""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from task_runtime import (
    SUCCESS_NOTIFY,
    TaskRuntimeError,
    atomic_write_json,
    atomic_write_text,
    read_json_object,
)


class GitHubSyncError(TaskRuntimeError):
    """Raised when a publication cannot be safely committed or pushed."""


def _text(value: Any) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def _markdown_link(label: Any, url: Any) -> str:
    safe_label = _text(label).replace("[", "\\[").replace("]", "\\]")
    safe_url = str(url or "").strip()
    if not safe_url.startswith(("https://", "http://")):
        raise GitHubSyncError("publication source URL must use http or https")
    return f"[{safe_label}]({safe_url})"


def _validated_cards(envelope: Dict[str, Any], expected_count: int) -> List[Dict[str, Any]]:
    final = envelope.get("final")
    result = envelope.get("result")
    if not isinstance(final, dict) or final.get("status") != SUCCESS_NOTIFY:
        raise GitHubSyncError("only finalized SUCCESS_NOTIFY results can be published")
    if not isinstance(result, dict) or result.get("status") != SUCCESS_NOTIFY:
        raise GitHubSyncError("published result must have SUCCESS_NOTIFY status")
    notification = result.get("notification")
    cards = notification.get("cards") if isinstance(notification, dict) else None
    if not isinstance(cards, list) or len(cards) != expected_count:
        raise GitHubSyncError(
            f"publication requires exactly {expected_count} finalized research cards"
        )
    for index, card in enumerate(cards, start=1):
        if not isinstance(card, dict) or card.get("template") != "research_item":
            raise GitHubSyncError(f"publication card {index} is not a research_item")
        sections = card.get("sections")
        if not isinstance(sections, list) or not sections:
            raise GitHubSyncError(f"publication card {index} has no summary sections")
        source = card.get("source")
        if not isinstance(source, dict):
            raise GitHubSyncError(f"publication card {index} has no source")
    return cards


def render_daily_publication(envelope: Dict[str, Any], *, expected_count: int) -> str:
    """Render every finalized card section into one GitHub-friendly Markdown page."""

    cards = _validated_cards(envelope, expected_count)
    scheduled_at = str(envelope.get("scheduled_at") or "")
    publication_date = scheduled_at[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication_date):
        raise GitHubSyncError("scheduled_at must provide a YYYY-MM-DD publication date")
    result = envelope["result"]
    notification = result["notification"]
    lines = [
        f"# Agent Memory 前沿论文日报 · {publication_date}",
        "",
        "> 本页由 Automation Hub 从已通过 Schema 与 Harness 校验的 Task C 最终结果自动归档。",
        "",
    ]
    summary = _text(result.get("summary"))
    body = _text(notification.get("body"))
    if summary:
        lines.extend([summary, ""])
    if body and body != summary:
        lines.extend([body, ""])
    lines.extend(["## 今日 Top 5", ""])

    for index, card in enumerate(cards, start=1):
        title = _text(card.get("title"))
        subtitle = _text(card.get("subtitle"))
        tag = _text(card.get("tag"))
        focus = card.get("focus")
        lines.extend([f"## {index}. {title}", ""])
        if subtitle:
            lines.extend([f"**中文译名：** {subtitle}", ""])
        if tag:
            lines.extend([f"**方向：** {tag}", ""])
        if isinstance(focus, dict):
            label = _text(focus.get("label"))
            value = _text(focus.get("value"))
            if label or value:
                lines.extend([f"**{label or '重点'}：** {value}", ""])

        fields = card.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                label = _text(field.get("label"))
                value = _text(field.get("value"))
                if label and value:
                    lines.append(f"- **{label}：** {value}")
            lines.append("")

        for section in card["sections"]:
            if not isinstance(section, dict):
                continue
            section_title = _text(section.get("title"))
            content = _text(section.get("content"))
            if section.get("collapsed"):
                lines.extend(
                    [
                        "<details>",
                        f"<summary>{section_title}</summary>",
                        "",
                        content,
                        "",
                        "</details>",
                        "",
                    ]
                )
            else:
                lines.extend([f"### {section_title}", "", content, ""])

        source = card["source"]
        lines.extend(
            [
                f"**原始来源：** {_markdown_link(source.get('label'), source.get('url'))}",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _run_git(repo_root: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = " ".join((completed.stderr or completed.stdout).split())[:1000]
        raise GitHubSyncError(
            f"git {args[0]} failed with exit code {completed.returncode}: {detail}"
        )
    return completed


def _publication_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = config.get("github_sync")
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    return value


def _publication_path(
    repo_root: Path, config: Dict[str, Any], envelope: Dict[str, Any]
) -> Path:
    sync_config = _publication_config(config)
    if sync_config is None:
        raise GitHubSyncError("GitHub sync is not enabled")
    scheduled_at = str(envelope.get("scheduled_at") or "")
    publication_date = scheduled_at[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication_date):
        raise GitHubSyncError("scheduled_at must provide a YYYY-MM-DD publication date")
    path = (repo_root / str(sync_config["directory"]) / f"{publication_date}.md").resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise GitHubSyncError("publication path must stay inside the repository") from exc
    return path


def _runtime_publications(state: Dict[str, Any]) -> Dict[str, Any]:
    runtime = state.setdefault("_runtime", {})
    if not isinstance(runtime, dict):
        raise GitHubSyncError("state._runtime must be a mapping")
    publications = runtime.setdefault("github_publications", {})
    if not isinstance(publications, dict):
        raise GitHubSyncError("state._runtime.github_publications must be a mapping")
    return publications


def _publish_one(
    *,
    repo_root: Path,
    sync_config: Dict[str, Any],
    publication_relative: str,
    publication_date: str,
) -> str:
    remote = str(sync_config["remote"])
    branch = str(sync_config["branch"])
    publication_path = repo_root / publication_relative
    if not publication_path.is_file():
        raise GitHubSyncError(f"pending publication is missing: {publication_relative}")

    current_branch = _run_git(repo_root, "branch", "--show-current").stdout.strip()
    if current_branch != branch:
        raise GitHubSyncError(
            f"GitHub sync requires branch {branch}, current branch is {current_branch or 'detached'}"
        )
    _run_git(repo_root, "remote", "get-url", remote)

    remote_head = _run_git(
        repo_root,
        "ls-remote",
        "--heads",
        remote,
        f"refs/heads/{branch}",
    ).stdout.strip()
    if remote_head:
        _run_git(
            repo_root,
            "fetch",
            "--quiet",
            remote,
            f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        )
        ancestry = _run_git(
            repo_root,
            "merge-base",
            "--is-ancestor",
            f"refs/remotes/{remote}/{branch}",
            "HEAD",
            allow_failure=True,
        )
        if ancestry.returncode != 0:
            raise GitHubSyncError(
                f"remote {remote}/{branch} is ahead or diverged; refusing to overwrite it"
            )

    changed = _run_git(
        repo_root, "status", "--porcelain", "--", publication_relative
    ).stdout.strip()
    if changed:
        _run_git(repo_root, "add", "--", publication_relative)
        _run_git(
            repo_root,
            "commit",
            "--only",
            "-m",
            f"docs(agent-memory): publish {publication_date} briefing",
            "--",
            publication_relative,
        )
    _run_git(repo_root, "push", remote, f"HEAD:refs/heads/{branch}")
    return _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()


def _mark_publication(
    state_path: Path,
    publication_relative: str,
    *,
    status: str,
    commit: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    state = read_json_object(state_path)
    publications = _runtime_publications(state)
    record = publications.get(publication_relative)
    if not isinstance(record, dict):
        raise GitHubSyncError(f"missing publication state: {publication_relative}")
    record["status"] = status
    record["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["commit"] = commit
    record["last_error"] = error
    atomic_write_json(state_path, state)


def _attempt_pending(
    *,
    repo_root: Path,
    config: Dict[str, Any],
    state_path: Path,
    publication_relative: str,
    publication_date: str,
) -> Dict[str, Any]:
    sync_config = _publication_config(config)
    if sync_config is None:
        return {"status": "disabled", "commit": None, "error": None}
    attempts = int(sync_config.get("retry_attempts", 1))
    last_error: Optional[str] = None
    for attempt in range(1, attempts + 1):
        try:
            commit = _publish_one(
                repo_root=repo_root,
                sync_config=sync_config,
                publication_relative=publication_relative,
                publication_date=publication_date,
            )
        except (OSError, GitHubSyncError) as exc:
            last_error = str(exc)[:2000]
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
            continue
        _mark_publication(
            state_path,
            publication_relative,
            status="published",
            commit=commit,
        )
        return {"status": "published", "commit": commit, "error": None}
    _mark_publication(
        state_path,
        publication_relative,
        status="pending",
        error=last_error,
    )
    return {"status": "pending", "commit": None, "error": last_error}


def enqueue_and_publish(
    *,
    repo_root: Path,
    config: Dict[str, Any],
    state_path: Path,
    output_json: Path,
) -> Dict[str, Any]:
    """Create a durable publication record, then commit and push it."""

    sync_config = _publication_config(config)
    if sync_config is None:
        return {"status": "disabled", "commit": None, "error": None}
    envelope = read_json_object(output_json)
    expected_count = int(config.get("workflow", {}).get("top_k", 5))
    markdown = render_daily_publication(envelope, expected_count=expected_count)
    publication_path = _publication_path(repo_root, config, envelope)
    publication_relative = str(publication_path.relative_to(repo_root.resolve()))
    publication_date = publication_path.stem
    atomic_write_text(publication_path, markdown)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = read_json_object(state_path)
    publications = _runtime_publications(state)
    publications[publication_relative] = {
        "status": "pending",
        "task_id": str(config["id"]),
        "output_json": str(output_json.resolve().relative_to(repo_root.resolve())),
        "publication_path": publication_relative,
        "publication_date": publication_date,
        "created_at": now,
        "updated_at": now,
        "commit": None,
        "last_error": None,
    }
    atomic_write_json(state_path, state)
    result = _attempt_pending(
        repo_root=repo_root,
        config=config,
        state_path=state_path,
        publication_relative=publication_relative,
        publication_date=publication_date,
    )
    return {**result, "publication_path": publication_relative}


def recover_pending_publications(
    *, repo_root: Path, config: Dict[str, Any], state_path: Path
) -> Dict[str, Any]:
    """Retry every durable GitHub publication without rerunning the Agent."""

    if _publication_config(config) is None:
        return {"status": "disabled", "recovered": 0, "failed": 0, "error": None}
    state = read_json_object(state_path)
    publications = _runtime_publications(state)
    pending = [
        (path, value)
        for path, value in publications.items()
        if isinstance(value, dict) and value.get("status") == "pending"
    ]
    recovered = 0
    failed = 0
    errors: List[str] = []
    for publication_relative, record in pending:
        result = _attempt_pending(
            repo_root=repo_root,
            config=config,
            state_path=state_path,
            publication_relative=str(publication_relative),
            publication_date=str(record.get("publication_date") or Path(publication_relative).stem),
        )
        if result["status"] == "published":
            recovered += 1
        else:
            failed += 1
            if result.get("error"):
                errors.append(str(result["error"]))
    return {
        "status": "ok" if failed == 0 else "pending",
        "recovered": recovered,
        "failed": failed,
        "error": "; ".join(errors)[:2000] or None,
    }


def has_pending_publications(state_path: Path) -> bool:
    state = read_json_object(state_path)
    publications = _runtime_publications(state)
    return any(
        isinstance(value, dict) and value.get("status") == "pending"
        for value in publications.values()
    )
