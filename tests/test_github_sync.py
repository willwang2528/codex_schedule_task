from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from validate_task import load_task_config, validate_task_config
import run_task
import scheduler


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def _card(rank: int) -> dict:
    return {
        "template": "research_item",
        "theme": "blue",
        "tag": f"方向 {rank}",
        "title": f"Agent Memory Paper {rank}",
        "subtitle": f"智能体记忆论文 {rank}",
        "focus": {"label": "重要性排名", "value": f"#{rank}"},
        "fields": [
            {"label": "日期", "value": "2026-09-05", "short": True},
            {"label": "状态", "value": "arXiv v1", "short": True},
            {
                "label": "Agent Memory 相关性",
                "value": f"记忆机制 {rank}",
                "short": False,
            },
            {
                "label": "入选理由",
                "value": f"证据充分 {rank}",
                "short": False,
            },
        ],
        "sections": [
            {
                "title": "一句话概述",
                "content": f"论文 {rank} 的核心结论。",
                "collapsed": False,
            },
            {
                "title": "论文摘要翻译",
                "content": f"论文 {rank} 的完整中文摘要。",
                "collapsed": True,
            },
            {
                "title": "现存问题",
                "content": f"论文 {rank} 解决的问题。",
                "collapsed": True,
            },
            {
                "title": "已有方法的不足",
                "content": f"论文 {rank} 对既有方法的判断。",
                "collapsed": True,
            },
            {
                "title": "当前方法为什么可行",
                "content": f"论文 {rank} 的方法和证据。",
                "collapsed": True,
            },
            {
                "title": "未来展望",
                "content": f"论文 {rank} 的边界与未来方向。",
                "collapsed": True,
            },
        ],
        "source": {
            "label": f"arXiv 2609.0000{rank}",
            "url": f"https://arxiv.org/abs/2609.0000{rank}",
        },
        "image_url": "",
    }


def _envelope() -> dict:
    return {
        "task_id": "agent-memory-frontier",
        "run_id": "run-20260905",
        "scheduled_at": "2026-09-05T09:00:00+08:00",
        "trigger_slot": "09:00",
        "result": {
            "status": "SUCCESS_NOTIFY",
            "summary": "今日五篇 Agent Memory 前沿论文已完成核验与排序。",
            "notification": {
                "title": "Agent Memory Top 5",
                "body": "今日研究重点涵盖写入、检索、更新与评测。",
                "cards": [_card(rank) for rank in range(1, 6)],
            },
        },
        "final": {"status": "SUCCESS_NOTIFY", "delivery_status": "ok"},
    }


def _load_github_sync_module():
    module_path = SCRIPTS / "github_sync.py"
    if not module_path.is_file():
        raise AssertionError("scripts/github_sync.py is missing")
    spec = importlib.util.spec_from_file_location("github_sync", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("scripts/github_sync.py cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TaskCGitHubSyncTests(unittest.TestCase):
    def test_only_task_c_enables_daily_github_publication(self) -> None:
        task_c_path = REPO_ROOT / "tasks" / "agent-memory-frontier" / "task.yaml"
        task_c = load_task_config(task_c_path)
        self.assertEqual(
            {
                "enabled": True,
                "directory": "publications/agent-memory-frontier",
                "remote": "origin",
                "branch": "main",
                "retry_attempts": 3,
            },
            task_c.get("github_sync"),
        )
        self.assertEqual([], validate_task_config(task_c, task_c_path, REPO_ROOT))

        for task_id in ("a-share-monitor", "apple-price-monitor"):
            config = load_task_config(REPO_ROOT / "tasks" / task_id / "task.yaml")
            self.assertNotIn("github_sync", config)

    def test_publisher_pushes_all_five_summaries_without_committing_other_changes(self) -> None:
        github_sync = _load_github_sync_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            repo = root / "repo"
            remote.mkdir()
            repo.mkdir()
            _git(remote, "init", "--bare")
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Automation Hub Test")
            _git(repo, "config", "user.email", "automation@example.test")
            _git(repo, "config", "commit.gpgsign", "false")
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            (repo / "unstaged.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt", "unstaged.txt")
            _git(repo, "commit", "-m", "initial")
            _git(repo, "remote", "add", "origin", str(remote))
            _git(repo, "push", "-u", "origin", "main")

            # Simulate unrelated user work in both index and working tree.
            (repo / "tracked.txt").write_text("user staged change\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            (repo / "unstaged.txt").write_text("user unstaged change\n", encoding="utf-8")

            output_json = repo / "outputs" / "agent-memory-frontier" / "run.json"
            output_json.parent.mkdir(parents=True)
            output_json.write_text(
                json.dumps(_envelope(), ensure_ascii=False), encoding="utf-8"
            )
            state_path = repo / "state" / "agent-memory-frontier.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}\n", encoding="utf-8")
            config = {
                "id": "agent-memory-frontier",
                "workflow": {"top_k": 5},
                "github_sync": {
                    "enabled": True,
                    "directory": "publications/agent-memory-frontier",
                    "remote": "origin",
                    "branch": "main",
                    "retry_attempts": 1,
                },
            }

            result = github_sync.enqueue_and_publish(
                repo_root=repo,
                config=config,
                state_path=state_path,
                output_json=output_json,
            )

            self.assertEqual("published", result["status"])
            publication = "publications/agent-memory-frontier/2026-09-05.md"
            remote_markdown = _git(repo, "show", f"origin/main:{publication}")
            for rank in range(1, 6):
                self.assertIn(f"Agent Memory Paper {rank}", remote_markdown)
                self.assertIn(f"论文 {rank} 的完整中文摘要。", remote_markdown)
                self.assertIn(f"论文 {rank} 的边界与未来方向。", remote_markdown)
                self.assertIn(
                    f"https://arxiv.org/abs/2609.0000{rank}", remote_markdown
                )
            self.assertEqual("base", _git(repo, "show", "origin/main:tracked.txt"))
            self.assertEqual("base", _git(repo, "show", "origin/main:unstaged.txt"))
            self.assertEqual("tracked.txt", _git(repo, "diff", "--cached", "--name-only"))
            self.assertEqual("unstaged.txt", _git(repo, "diff", "--name-only"))

            state = json.loads(state_path.read_text(encoding="utf-8"))
            publication_state = state["_runtime"]["github_publications"][publication]
            self.assertEqual("published", publication_state["status"])
            self.assertEqual(result["commit"], publication_state["commit"])

    def test_failed_push_stays_pending_and_can_be_recovered(self) -> None:
        github_sync = _load_github_sync_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.name", "Automation Hub Test")
            _git(repo, "config", "user.email", "automation@example.test")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "base.txt")
            _git(repo, "commit", "-m", "initial")
            output_json = repo / "outputs" / "run.json"
            output_json.parent.mkdir()
            output_json.write_text(
                json.dumps(_envelope(), ensure_ascii=False), encoding="utf-8"
            )
            state_path = repo / "state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            config = {
                "id": "agent-memory-frontier",
                "workflow": {"top_k": 5},
                "github_sync": {
                    "enabled": True,
                    "directory": "publications/agent-memory-frontier",
                    "remote": "missing",
                    "branch": "main",
                    "retry_attempts": 1,
                },
            }

            failed = github_sync.enqueue_and_publish(
                repo_root=repo,
                config=config,
                state_path=state_path,
                output_json=output_json,
            )
            self.assertEqual("pending", failed["status"])

            remote = root / "recover-remote.git"
            remote.mkdir()
            _git(remote, "init", "--bare")
            _git(repo, "remote", "add", "missing", str(remote))
            recovered = github_sync.recover_pending_publications(
                repo_root=repo,
                config=config,
                state_path=state_path,
            )
            self.assertEqual(1, recovered["recovered"])
            self.assertEqual(0, recovered["failed"])
            self.assertIn(
                "Agent Memory Paper 5",
                _git(
                    repo,
                    "show",
                    "missing/main:publications/agent-memory-frontier/2026-09-05.md",
                ),
            )

    def test_run_task_hands_a_successful_task_c_output_to_the_publisher(self) -> None:
        config = {
            "id": "agent-memory-frontier",
            "github_sync": {"enabled": True},
        }
        production_result = {
            "status": "SUCCESS_NOTIFY",
            "output_json": "outputs/agent-memory-frontier/run.json",
        }
        with patch.object(
            run_task.github_sync,
            "enqueue_and_publish",
            return_value={
                "status": "published",
                "publication_path": "publications/agent-memory-frontier/2026-09-05.md",
                "commit": "abc123",
                "error": None,
            },
        ) as publish:
            result = run_task._publish_production_result(
                repo_root=REPO_ROOT,
                config=config,
                state_path=REPO_ROOT / "state" / "agent-memory-frontier.json",
                production_result=production_result,
            )
        self.assertEqual("published", result["status"])
        self.assertEqual(
            REPO_ROOT / "outputs" / "agent-memory-frontier" / "run.json",
            publish.call_args.kwargs["output_json"],
        )

    def test_delivery_dry_run_never_pushes_a_github_publication(self) -> None:
        config = {
            "id": "agent-memory-frontier",
            "github_sync": {"enabled": True},
        }
        with patch.object(run_task.github_sync, "enqueue_and_publish") as publish:
            result = run_task._publish_production_result(
                repo_root=REPO_ROOT,
                config=config,
                state_path=REPO_ROOT / "state" / "agent-memory-frontier.json",
                production_result={
                    "status": "SUCCESS_NOTIFY",
                    "output_json": "outputs/agent-memory-frontier/run.json",
                },
                dry_run=True,
            )
        self.assertEqual("dry_run", result["status"])
        publish.assert_not_called()

    def test_scheduler_detects_a_pending_github_publication_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            state_path = repo / "state" / "agent-memory-frontier.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "_runtime": {
                            "notifications": {},
                            "github_publications": {
                                "publications/agent-memory-frontier/2026-09-05.md": {
                                    "status": "pending"
                                }
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            task = {"state_path": "state/agent-memory-frontier.json"}
            self.assertTrue(scheduler._has_pending_delivery(repo, task))


if __name__ == "__main__":
    unittest.main()
