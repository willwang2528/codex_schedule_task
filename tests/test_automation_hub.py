from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Dict
from unittest.mock import patch
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import feishu_send
import feishu_cards
import manage_scheduler
import production_runner
import scheduler
from feishu_send import FeishuDeliveryError
from production_runner import execute_production_task, recover_pending_delivery
from scheduler import discover_tasks, due_runs
from task_runtime import (
    FAILED,
    SKIPPED,
    SUCCESS_NO_NOTIFY,
    SUCCESS_NOTIFY,
    atomic_write_json,
    build_agent_prompt,
    make_run_id,
    read_json_object,
    run_lock,
)
from validate_task import load_task_config, validate_all_tasks, validate_task_config


def copy_result_schema(repo: Path) -> None:
    (repo / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "config" / "task-result.schema.json",
        repo / "config" / "task-result.schema.json",
    )


def structured_result(
    status: str,
    *,
    updates: Dict[str, Any] | None = None,
    event_key: str = "",
    error: str = "",
    cards: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    should_notify = status == SUCCESS_NOTIFY
    state_updates = [
        {
            "namespace": str(namespace),
            "operation": "upsert",
            "value_json": json.dumps(value, ensure_ascii=False, sort_keys=True),
        }
        for namespace, value in (updates or {}).items()
    ]
    return {
        "status": status,
        "should_notify": should_notify,
        "summary": "completed" if status in {SUCCESS_NOTIFY, SUCCESS_NO_NOTIFY} else "",
        "output_markdown": f"# {status}",
        "state_updates": state_updates,
        "notification": {
            "title": "Important update" if should_notify else "",
            "body": "Stable business event" if should_notify else "",
            "event_key": event_key if should_notify else "",
            "cards": cards or [],
        },
        "skip_reason": "non-trading-day" if status == SKIPPED else "",
        "error": error if status == FAILED else "",
        "source_metadata": [
            {
                "source": "primary-source",
                "data_timestamp": "2026-08-19T09:20:00+08:00",
                "freshness": "current",
            }
        ],
    }


def structured_result_v2(
    status: str,
    *,
    updates: Dict[str, Any] | None = None,
    event_key: str = "",
    error: str = "",
    cards: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    value = structured_result(
        status,
        updates=updates,
        event_key=event_key,
        error=error,
        cards=cards,
    )
    return value


def semantic_card(
    template: str,
    *,
    rank: int = 1,
    image_url: str = "",
) -> Dict[str, Any]:
    fields = [
        {"label": "Status", "value": "Published", "short": True},
        {"label": "Date", "value": "2026-08-19", "short": True},
    ]
    sections = [
        {"title": "Summary", "content": "Verified contribution.", "collapsed": False},
        {"title": "Why it matters", "content": "Improves agent memory.", "collapsed": True},
        {"title": "Selection", "content": "Strong evidence.", "collapsed": True},
    ]
    if template == "research_item":
        title = "Memory-R1: Learning to Manage Agent Memory"
        subtitle = "Memory-R1：学习管理智能体记忆"
        fields = [
            {"label": "日期", "value": "2026-08-19", "short": True},
            {"label": "状态", "value": "正式发表", "short": True},
            {
                "label": "Agent Memory 相关性",
                "value": "改善长期记忆的写入与检索。",
                "short": False,
            },
            {
                "label": "入选理由",
                "value": "方法贡献明确且有一手实验证据。",
                "short": False,
            },
        ]
        sections = [
            {
                "title": "一句话概述",
                "content": "该研究解决长程任务中记忆写入与检索失配的问题，并通过强化学习训练智能体判断何时写入、更新和读取记忆。",
                "collapsed": False,
            },
            {
                "title": "论文摘要翻译",
                "content": "大型语言模型智能体需要在长期交互中管理不断累积的记忆，但固定规则难以同时适应写入、更新和检索。论文提出一种强化学习方法，把记忆操作作为可学习决策并与任务执行联合优化。作者在受控任务中比较该方法与既有记忆管理策略，同时说明当前验证范围仍受所用模型和环境限制。",
                "collapsed": True,
            },
            {
                "title": "现存问题",
                "content": "长程 Agent 会积累大量轨迹，关键经验难以稳定进入后续决策。",
                "collapsed": True,
            },
            {
                "title": "已有方法的不足",
                "content": "仅依赖相似度检索会忽略经验的可执行结构与适用边界。",
                "collapsed": True,
            },
            {
                "title": "当前方法为什么可行",
                "content": "结构化索引改变了记忆组织和调用路径，并由跨任务对照实验支持。",
                "collapsed": True,
            },
            {
                "title": "未来展望",
                "content": "作者提出扩展至更多模型与更长时间跨度，当前还缺少真实环境验证。",
                "collapsed": True,
            },
        ]
    return {
        "template": template,
        "title": title if template == "research_item" else f"Card {rank}",
        "subtitle": subtitle if template == "research_item" else "2026-08-19 · verified",
        "theme": "blue" if template == "research_item" else "green",
        "tag": f"TOP {rank}" if template == "research_item" else "ALERT",
        "focus": {"value": f"#{rank}", "label": "Top research item"},
        "fields": fields,
        "sections": sections,
        "source": {"label": "Primary source", "url": "https://example.com/paper"},
        "image_url": image_url,
    }


def market_dashboard_cards() -> list[Dict[str, Any]]:
    cards = [semantic_card("market_dashboard", rank=index) for index in range(1, 4)]
    tags = ("盘面总览", "情绪与主线", "异动与风险")
    themes = ("blue", "orange", "yellow")
    focus_values = ("指数分化", "结构轮动", "关注炸板扩散")
    for card, tag, theme, focus_value in zip(cards, tags, themes, focus_values):
        card["tag"] = tag
        card["theme"] = theme
        card["focus"] = {"value": focus_value, "label": "已核验的阶段判断"}
        titles = feishu_cards.MARKET_SECTION_TITLES_BY_TAG[tag]
        card["sections"] = [
            {
                "title": title,
                "content": (
                    "指数、市场广度与成交共同支持当前阶段判断。"
                    if index == 0
                    else "详细证据、变化和核验边界。"
                ),
                "collapsed": index > 0,
            }
            for index, title in enumerate(titles)
        ]
    cards[0]["fields"] = [
        {"label": label, "value": "已核验", "short": True}
        for label in (
            "上证指数",
            "深证成指",
            "创业板指",
            "科创50",
            "沪深成交额",
            "上涨 / 下跌 / 平盘",
            "涨停 / 跌停 / 炸板",
        )
    ]
    return cards


class RepositoryContractTests(unittest.TestCase):
    def test_all_tasks_validate_and_are_discovered(self) -> None:
        reports = validate_all_tasks(REPO_ROOT)
        self.assertEqual(4, len(reports))
        self.assertTrue(all(report["valid"] for report in reports), reports)
        tasks = {task["id"]: task for task in discover_tasks(REPO_ROOT)}
        self.assertEqual(
            {
                "a-share-monitor",
                "apple-price-monitor",
                "agent-memory-frontier",
                "smoke-test",
            },
            set(tasks),
        )
        self.assertFalse(tasks["smoke-test"]["enabled"])
        self.assertTrue(tasks["a-share-monitor"]["enabled"])

    def test_task_c_prompt_requires_fast_chinese_paper_comprehension(self) -> None:
        prompt = (
            REPO_ROOT / "tasks" / "agent-memory-frontier" / "TASK.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Stage 1 — Build a broad candidate pool", prompt)
        self.assertIn("Stage 2 — Independently validate", prompt)
        self.assertIn("optimize only the Feishu card presentation", prompt)
        for title in feishu_cards.RESEARCH_SECTION_TITLES:
            self.assertIn(f"`{title}`", prompt)

    def test_structured_output_schema_accepts_the_renderable_research_card(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "config" / "task-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        sections_limit = schema["properties"]["notification"]["properties"][
            "cards"
        ]["items"]["properties"]["sections"]["maxItems"]
        renderable_sections = len(semantic_card("research_item")["sections"])

        self.assertGreaterEqual(
            sections_limit,
            renderable_sections,
            "Codex output schema must accept every section the renderer accepts",
        )

    def test_structured_output_schema_rejects_unknown_state_namespaces(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "config" / "task-result.schema.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("state_updates", schema["required"])
        self.assertNotIn("state_updates_json", schema["properties"])
        state_updates = schema["properties"]["state_updates"]
        self.assertEqual("array", state_updates["type"])
        operation = state_updates["items"]
        self.assertEqual(False, operation["additionalProperties"])
        self.assertEqual(
            set(operation["properties"]),
            set(operation["required"]),
        )
        self.assertNotIn(
            "last_run_at",
            operation["properties"]["namespace"]["enum"],
        )

    def test_structured_output_schema_is_compatible_with_strict_mode(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "config" / "task-result.schema.json").read_text(
                encoding="utf-8"
            )
        )

        def assert_strict_object(node: Any, path: str = "") -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    properties = node.get("properties", {})
                    self.assertEqual(
                        False,
                        node.get("additionalProperties"),
                        f"{path or '/'} must reject additional properties",
                    )
                    self.assertEqual(
                        set(properties),
                        set(node.get("required", [])),
                        f"{path or '/'} must require every property",
                    )
                for key, child in node.items():
                    assert_strict_object(child, f"{path}/{key}")
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    assert_strict_object(child, f"{path}/{index}")

        assert_strict_object(schema)

    def test_task_c_execution_prompt_requires_unique_historical_backfill(self) -> None:
        business_prompt = (
            REPO_ROOT / "tasks" / "agent-memory-frontier" / "TASK.md"
        ).read_text(encoding="utf-8")
        prompt = build_agent_prompt(
            task_id="agent-memory-frontier",
            task_name="Agent Memory 前沿增量追踪",
            prompt_path="tasks/agent-memory-frontier/TASK.md",
            prompt_text=business_prompt,
            state_path="state/agent-memory-frontier.json",
            state_context={
                "reported_items": ["arxiv:2608.20274"],
                "paper_registry": {
                    "arxiv:2608.20274": {
                        "primary_source": "https://arxiv.org/abs/2608.20274"
                    }
                },
            },
            scheduled_at="2026-08-25T09:00:00+08:00",
            trigger_slot="09:00",
            timezone_name="Asia/Shanghai",
            presentation_instruction="Return five research cards.",
        )

        self.assertIn('"reported_items": ["arxiv:2608.20274"]', prompt)
        self.assertIn(
            "Selection priority is strict: (1) quality and deduplication; "
            "(2) recency; (3) completing all five slots.",
            prompt,
        )
        self.assertIn("Exclude every item already present in `reported_items`", prompt)
        self.assertIn("move the publication cutoff backward", prompt)
        self.assertIn("Do not reuse a previously reported item", prompt)

    def test_a_share_prompt_monitors_every_configured_trigger(self) -> None:
        config = load_task_config(REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml")
        prompt = (REPO_ROOT / "tasks" / "a-share-monitor" / "TASK.md").read_text(
            encoding="utf-8"
        )
        for trigger in config["schedule"]["triggers"]:
            self.assertIn(f"`{trigger}`", prompt)
        self.assertEqual(
            ["09:35", "11:20", "15:01"],
            config["delivery"]["notification_triggers"],
        )
        self.assertIn("正常交易日这三个时点返回 `SUCCESS_NOTIFY`", prompt)
        self.assertIn("其余时点只采集和保存数据", prompt)
        self.assertIn("不得因为“尚未收盘”而跳过", prompt)
        self.assertIn("股民首屏阅读优先级", prompt)
        self.assertIn("红涨绿跌", prompt)
        for titles in feishu_cards.MARKET_SECTION_TITLES_BY_TAG.values():
            for title in titles:
                self.assertIn(f"`{title}`", prompt)
        self.assertIn("SUCCESS_NOTIFY", prompt)
        self.assertIn("Deterministic workflow evidence", prompt)
        self.assertEqual(
            config["schedule"]["triggers"],
            config["delivery"]["failure_alert"]["trigger_slots"],
        )
        self.assertTrue(config["delivery"]["failure_alert"]["enabled"])
        self.assertEqual(
            ["11:20", "15:01"],
            config["delivery"].get("completion_monitor", {}).get("trigger_slots"),
        )
        self.assertTrue(config["delivery"].get("readback", {}).get("enabled"))
        self.assertEqual(
            ["intraday_state", "completed_trigger_slots_add", "trading_date"],
            config["state"].get("allowed_update_keys"),
        )

    def test_failure_alert_trigger_slots_must_be_scheduled(self) -> None:
        config_path = REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml"
        config = load_task_config(config_path)
        config["delivery"]["failure_alert"] = {
            "enabled": True,
            "trigger_slots": ["12:34"],
        }

        errors = validate_task_config(config, config_path, REPO_ROOT)

        self.assertIn(
            "delivery.failure_alert.trigger_slots must be a subset of schedule.triggers: 12:34",
            errors,
        )

    def test_task_state_namespaces_must_exist_in_result_schema(self) -> None:
        config_path = REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml"
        config = load_task_config(config_path)
        config["state"]["allowed_update_keys"].append("invented_namespace")

        errors = validate_task_config(config, config_path, REPO_ROOT)

        self.assertIn(
            "state.allowed_update_keys must be declared in task-result schema: invented_namespace",
            errors,
        )

    def test_daily_archive_triggers_must_be_notification_enabled(self) -> None:
        config_path = REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml"
        config = load_task_config(config_path)
        config["delivery"]["daily_archive"] = {
            "enabled": True,
            "trigger_slots": ["14:30"],
        }

        errors = validate_task_config(config, config_path, REPO_ROOT)

        self.assertIn(
            "delivery.daily_archive.trigger_slots must be a subset of delivery.notification_triggers: 14:30",
            errors,
        )

    def test_daily_archive_config_rejects_each_malformed_shape(self) -> None:
        config_path = REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml"
        base_config = load_task_config(config_path)
        cases = (
            ([], "delivery.daily_archive must be a mapping"),
            (
                {"enabled": "yes", "trigger_slots": ["15:01"]},
                "delivery.daily_archive.enabled must be true or false",
            ),
            (
                {"enabled": True, "trigger_slots": []},
                "delivery.daily_archive.trigger_slots must be a non-empty inline list",
            ),
            (
                {"enabled": True, "trigger_slots": ["25:00"]},
                "delivery.daily_archive.trigger_slots values must use HH:MM (24-hour time)",
            ),
            (
                {"enabled": True, "trigger_slots": ["15:01", "15:01"]},
                "delivery.daily_archive.trigger_slots must not contain duplicates",
            ),
        )

        for archive_config, expected_error in cases:
            with self.subTest(archive_config=archive_config):
                config = copy.deepcopy(base_config)
                config["delivery"]["daily_archive"] = archive_config

                errors = validate_task_config(config, config_path, REPO_ROOT)

                self.assertIn(expected_error, errors)

    def test_task_c_execution_budget_remains_two_hours(self) -> None:
        config = load_task_config(
            REPO_ROOT / "tasks" / "agent-memory-frontier" / "task.yaml"
        )
        self.assertEqual(2 * 60 * 60, config["execution"]["timeout_seconds"])

    def test_scheduler_has_exact_daily_slots_and_ignores_disabled_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            isolated_root = Path(directory).resolve()
            shutil.copytree(REPO_ROOT / "tasks", isolated_root / "tasks")
            copy_result_schema(isolated_root)
            (isolated_root / "scripts").mkdir()
            shutil.copy2(
                REPO_ROOT / "scripts" / "smoke_test.py",
                isolated_root / "scripts" / "smoke_test.py",
            )
            tasks = {task["id"]: task for task in discover_tasks(isolated_root)}
            self.assertEqual(
                [
                    "09:20",
                    "09:25",
                    "09:35",
                    "09:45",
                    "11:20",
                    "13:15",
                    "14:30",
                    "15:01",
                ],
                tasks["a-share-monitor"]["triggers"],
            )
            self.assertEqual(["10:00"], tasks["apple-price-monitor"]["triggers"])
            self.assertEqual(["09:00"], tasks["agent-memory-frontier"]["triggers"])

            weekend = datetime(
                2026, 8, 22, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai")
            )
            due = due_runs(isolated_root, weekend)
            self.assertIn("a-share-monitor", {item["task"] for item in due})
            self.assertNotIn("smoke-test", {item["task"] for item in due})

    def test_production_tasks_use_the_expected_card_profiles(self) -> None:
        expected = {
            "a-share-monitor": "market_dashboard_card",
            "apple-price-monitor": "price_alert_cards",
            "agent-memory-frontier": "research_top5_cards",
        }
        for task_id, presentation in expected.items():
            config = load_task_config(REPO_ROOT / "tasks" / task_id / "task.yaml")
            self.assertEqual(presentation, config["delivery"]["presentation"])

    def test_only_a_share_monitor_overrides_the_default_feishu_chat(self) -> None:
        expected_route = "FEISHU_CHAT_ID_A_SHARE_MONITOR_SCHEDULE_TASK"
        a_share = load_task_config(REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml")
        self.assertEqual(expected_route, a_share["delivery"]["chat_id_env"])
        for task_id in ("apple-price-monitor", "agent-memory-frontier"):
            config = load_task_config(REPO_ROOT / "tasks" / task_id / "task.yaml")
            self.assertNotIn("chat_id_env", config["delivery"])

    def test_launch_agent_prevents_idle_system_sleep(self) -> None:
        payload = manage_scheduler.plist_payload(REPO_ROOT)
        arguments = payload["ProgramArguments"]
        self.assertEqual(["/usr/bin/caffeinate", "-i"], arguments[:2])
        self.assertIn(str(REPO_ROOT / "scripts" / "scheduler.py"), arguments)


class SchedulerRegressionTests(unittest.TestCase):
    def test_pending_delivery_recovers_before_same_task_due_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            shutil.copytree(REPO_ROOT / "tasks", repo / "tasks")
            copy_result_schema(repo)
            scripts = repo / "scripts"
            scripts.mkdir()
            shutil.copy2(REPO_ROOT / "scripts" / "smoke_test.py", scripts)
            (scripts / "run_task.py").write_text(
                "import json\n"
                "import sys\n"
                "mode = 'recover' if '--recover-pending' in sys.argv else 'execute'\n"
                "print(json.dumps({'mode': mode, 'task_id': sys.argv[1]}))\n",
                encoding="utf-8",
            )
            atomic_write_json(
                repo / "state" / "a-share-monitor.json",
                {
                    "_runtime": {
                        "processed_runs": {},
                        "health_checks": {
                            make_run_id(
                                "a-share-monitor",
                                "2026-08-19T11:20:00+08:00",
                                "11:20",
                            ): {"status": "healthy"}
                        },
                        "notifications": {
                            "pending-market": {
                                "status": "pending",
                                "event_key": "market:pending",
                            }
                        },
                    }
                },
            )
            for task_id, slot in (
                ("agent-memory-frontier", "09:00"),
                ("apple-price-monitor", "10:00"),
            ):
                hour, minute = (int(part) for part in slot.split(":"))
                scheduled = datetime(
                    2026,
                    8,
                    19,
                    hour,
                    minute,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ).isoformat(timespec="seconds")
                run_id = make_run_id(task_id, scheduled, slot)
                atomic_write_json(
                    repo / "state" / f"{task_id}.json",
                    {
                        "_runtime": {
                            "processed_runs": {
                                run_id: {"terminal": True, "status": SUCCESS_NO_NOTIFY}
                            },
                            "notifications": {},
                        }
                    },
                )
            at = datetime(
                2026, 8, 19, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")
            )

            result = scheduler.run_once(
                repo,
                at=at,
                dry_run=False,
                recover_pending=True,
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["failed_count"])
        self.assertEqual(
            [
                {"mode": "recover", "task_id": "a-share-monitor"},
                {"mode": "execute", "task_id": "a-share-monitor"},
            ],
            [
                {"mode": item["mode"], "task_id": item["task_id"]}
                for item in result["results"]
            ],
        )

    def test_missing_notification_run_after_grace_creates_alert_and_health_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            task_root = repo / "tasks" / "a-share-monitor"
            task_root.parent.mkdir(parents=True)
            shutil.copytree(REPO_ROOT / "tasks" / "a-share-monitor", task_root)
            copy_result_schema(repo)
            (repo / "scripts").mkdir()
            shutil.copy2(
                REPO_ROOT / "scripts" / "smoke_test.py",
                repo / "scripts" / "smoke_test.py",
            )
            atomic_write_json(
                repo / "state" / "a-share-monitor.json",
                {"_runtime": {"processed_runs": {}, "notifications": {}}},
            )
            at = datetime(
                2026, 8, 28, 11, 31, tzinfo=ZoneInfo("Asia/Shanghai")
            )

            with patch.object(
                production_runner,
                "send_message",
                return_value={"status": "ok", "message_id": "om_missing_run"},
            ), patch.object(
                production_runner,
                "read_message",
                return_value={
                    "status": "ok",
                    "message_id": "om_missing_run",
                    "chat_id": "oc_market",
                    "msg_type": "post",
                },
                create=True,
            ):
                result = scheduler.run_once(
                    repo,
                    at=at,
                    dry_run=False,
                    recover_pending=True,
                )

            state = read_json_object(repo / "state" / "a-share-monitor.json")
            alerts = [
                value
                for value in state["_runtime"]["notifications"].values()
                if value.get("purpose") == "failure_alert"
            ]
            health_log = repo / "logs" / "scheduler" / "health.jsonl"
            health_log_exists = health_log.is_file()

        self.assertEqual(1, len(result.get("monitoring", [])))
        self.assertEqual("missing_run", result["monitoring"][0]["reason"])
        self.assertEqual(1, len(alerts))
        self.assertEqual("sent", alerts[0]["status"])
        self.assertTrue(health_log_exists)


class TaskBehaviorRegressionTests(unittest.TestCase):
    def test_task_a_real_config_enforces_collection_send_and_archive_matrix(self) -> None:
        config = load_task_config(
            REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml"
        )
        cases = (
            ("09:20", SUCCESS_NO_NOTIFY, 0, []),
            ("09:35", SUCCESS_NOTIFY, 3, []),
            ("11:20", SUCCESS_NOTIFY, 3, []),
            ("15:01", SUCCESS_NOTIFY, 3, ["om_15_01_1"]),
        )

        for slot, expected_status, expected_sends, expected_pins in cases:
            with self.subTest(slot=slot), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory).resolve()
                prompt_path = repo / "tasks" / "a-share-monitor" / "TASK.md"
                prompt_path.parent.mkdir(parents=True)
                shutil.copy2(
                    REPO_ROOT / "tasks" / "a-share-monitor" / "TASK.md",
                    prompt_path,
                )
                state_path = repo / "state" / "a-share-monitor.json"
                atomic_write_json(
                    state_path,
                    {
                        "schema_version": 1,
                        "task_id": "a-share-monitor",
                        "state_version": 0,
                        "_runtime": {"processed_runs": {}, "notifications": {}},
                    },
                )
                output_directory = repo / "outputs" / "a-share-monitor"
                sends = []
                pins = []
                hour, minute = (int(part) for part in slot.split(":"))
                scheduled_at = datetime(
                    2026,
                    8,
                    19,
                    hour,
                    minute,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                )
                agent_result = (
                    structured_result(
                        SUCCESS_NOTIFY,
                        event_key=f"market:2026-08-19:{slot}",
                        cards=market_dashboard_cards(),
                    )
                    if expected_status == SUCCESS_NOTIFY
                    else structured_result(
                        SUCCESS_NO_NOTIFY,
                        updates={
                            "intraday_state": {
                                f"2026-08-19|{slot}": {"collector": "verified"}
                            }
                        },
                    )
                )

                with patch.object(
                    production_runner,
                    "_load_workflow_evidence",
                    return_value={"status": "ok", "collector": "fixture"},
                ), patch.object(
                    production_runner,
                    "read_message",
                    side_effect=lambda message_id, **kwargs: {
                        "status": "ok",
                        "message_id": message_id,
                        "chat_id": "oc_market",
                        "msg_type": "interactive",
                    },
                ):
                    result = execute_production_task(
                        repo_root=repo,
                        config=config,
                        prompt_path=prompt_path,
                        state_path=state_path,
                        output_directory=output_directory,
                        scheduled_at=scheduled_at,
                        trigger_slot=slot,
                        agent_runner=lambda prompt, task_config, root: agent_result,
                        delivery_sender=lambda text, **kwargs: (
                            sends.append(kwargs)
                            or {
                                "status": "ok",
                                "message_id": f"om_{slot.replace(':', '_')}_{len(sends)}",
                            }
                        ),
                        pin_sender=lambda message_id: (
                            pins.append(message_id)
                            or {"status": "ok", "message_id": message_id}
                        ),
                    )

                state = read_json_object(state_path)
                self.assertEqual(expected_status, result["status"])
                self.assertEqual(expected_sends, len(sends))
                self.assertEqual(expected_pins, pins)
                self.assertEqual(1, state["state_version"])
                self.assertTrue((repo / result["output_json"]).is_file())
                if slot == "09:20":
                    self.assertEqual(
                        "verified",
                        state["intraday_state"]["2026-08-19|09:20"]["collector"],
                    )
                    self.assertEqual({}, state["_runtime"]["notifications"])
                elif slot == "15:01":
                    notification = next(
                        iter(state["_runtime"]["notifications"].values())
                    )
                    self.assertEqual(
                        "pinned", notification["daily_archive"]["status"]
                    )
                    self.assertEqual(
                        "om_15_01_1",
                        notification["daily_archive"]["message_id"],
                    )
                else:
                    notification = next(
                        iter(state["_runtime"]["notifications"].values())
                    )
                    self.assertNotIn("daily_archive", notification)

    def test_task_c_successful_daily_run_retries_silence_and_sends_five_cards(self) -> None:
        config = load_task_config(
            REPO_ROOT / "tasks" / "agent-memory-frontier" / "task.yaml"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            prompt_path = repo / "tasks" / "agent-memory-frontier" / "TASK.md"
            prompt_path.parent.mkdir(parents=True)
            shutil.copy2(
                REPO_ROOT / "tasks" / "agent-memory-frontier" / "TASK.md",
                prompt_path,
            )
            state_path = repo / "state" / "agent-memory-frontier.json"
            atomic_write_json(
                state_path,
                {
                    "schema_version": 1,
                    "task_id": "agent-memory-frontier",
                    "state_version": 0,
                    "_runtime": {"processed_runs": {}, "notifications": {}},
                },
            )
            attempts = []
            deliveries = []

            def agent(
                prompt: str, task_config: Dict[str, Any], root: Path
            ) -> Dict[str, Any]:
                attempts.append(prompt)
                if len(attempts) == 1:
                    return structured_result(SUCCESS_NO_NOTIFY)
                return structured_result(
                    SUCCESS_NOTIFY,
                    event_key="agent-memory:2026-08-24:daily-top5",
                    cards=[
                        semantic_card("research_item", rank=rank)
                        for rank in range(1, 6)
                    ],
                )

            result = execute_production_task(
                repo_root=repo,
                config=config,
                prompt_path=prompt_path,
                state_path=state_path,
                output_directory=repo / "outputs" / "agent-memory-frontier",
                scheduled_at=datetime(
                    2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
                trigger_slot="09:00",
                agent_runner=agent,
                delivery_sender=lambda text, **kwargs: (
                    deliveries.append(kwargs)
                    or {
                        "status": "ok",
                        "message_id": f"om_research_{len(deliveries)}",
                    }
                ),
            )

        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(2, len(attempts))
        self.assertEqual(5, len(deliveries))
        self.assertTrue(result["notification_sent"])

    def test_task_b_disabled_task_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            shutil.copytree(REPO_ROOT / "tasks", repo / "tasks")
            copy_result_schema(repo)
            (repo / "scripts").mkdir()
            shutil.copy2(REPO_ROOT / "scripts" / "smoke_test.py", repo / "scripts")

            due = due_runs(
                repo,
                datetime(
                    2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
            )

        self.assertNotIn("apple-price-monitor", {item["task"] for item in due})

    def test_task_b_delivery_is_disabled_even_when_manually_executed(self) -> None:
        config = load_task_config(
            REPO_ROOT / "tasks" / "apple-price-monitor" / "task.yaml"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            prompt_path = repo / "tasks" / "apple-price-monitor" / "TASK.md"
            prompt_path.parent.mkdir(parents=True)
            shutil.copy2(
                REPO_ROOT / "tasks" / "apple-price-monitor" / "TASK.md",
                prompt_path,
            )
            state_path = repo / "state" / "apple-price-monitor.json"
            atomic_write_json(
                state_path,
                {
                    "schema_version": 1,
                    "task_id": "apple-price-monitor",
                    "state_version": 0,
                    "_runtime": {"processed_runs": {}, "notifications": {}},
                },
            )
            deliveries = []

            result = execute_production_task(
                repo_root=repo,
                config=config,
                prompt_path=prompt_path,
                state_path=state_path,
                output_directory=repo / "outputs" / "apple-price-monitor",
                scheduled_at=datetime(
                    2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                ),
                trigger_slot="10:00",
                agent_runner=lambda prompt, task_config, root: structured_result(
                    SUCCESS_NOTIFY,
                    event_key="apple-price:manual-check",
                    cards=[semantic_card("price_alert")],
                ),
                delivery_sender=lambda text, **kwargs: (
                    deliveries.append(kwargs)
                    or {"status": "ok", "message_id": "om_must_not_send"}
                ),
            )

        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual("skipped", result["delivery_status"])
        self.assertEqual([], deliveries)


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name)
        (self.repo / "tasks" / "test-task").mkdir(parents=True)
        self.prompt_path = self.repo / "tasks" / "test-task" / "TASK.md"
        self.prompt_path.write_text("Do the exact business task.", encoding="utf-8")
        self.state_path = self.repo / "state" / "test-task.json"
        self.output_directory = self.repo / "outputs" / "test-task"
        atomic_write_json(
            self.state_path,
            {
                "schema_version": 1,
                "task_id": "test-task",
                "state_version": 0,
                "safe_baseline": 100,
                "notification_history": [],
                "_runtime": {"processed_runs": {}, "notifications": {}},
            },
        )
        self.config: Dict[str, Any] = {
            "id": "test-task",
            "name": "Test Task",
            "enabled": True,
            "schedule": {"timezone": "Asia/Shanghai", "triggers": ["09:20"]},
            "workflow": {"type": "codex_structured"},
            "execution": {"timeout_seconds": 30, "retry_attempts": 2},
            "delivery": {
                "type": "feishu",
                "enabled": True,
                "policy": "conditional",
                "retry_attempts": 1,
            },
            "state": {"enabled": True, "context_recent_items": 25},
        }
        self.scheduled_at = datetime(
            2026, 8, 19, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai")
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def execute(
        self,
        result: Dict[str, Any],
        *,
        sender: Any = None,
        pin_sender: Any = None,
        scheduled_at: datetime | None = None,
        trigger_slot: str = "09:20",
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if sender is not None:
            kwargs["delivery_sender"] = sender
        if pin_sender is not None:
            kwargs["pin_sender"] = pin_sender
        return execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=scheduled_at or self.scheduled_at,
            trigger_slot=trigger_slot,
            agent_runner=lambda prompt, config, root: result,
            **kwargs,
        )

    def test_success_no_notify_loads_prompt_persists_state_and_outputs(self) -> None:
        captured: Dict[str, str] = {}

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            captured["prompt"] = prompt
            return structured_result(
                SUCCESS_NO_NOTIFY, updates={"last_valid_price": 88}
            )

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
        )
        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual(88, state["last_valid_price"])
        self.assertEqual(1, state["state_version"])
        self.assertIn("Do the exact business task.", captured["prompt"])
        self.assertIn('"safe_baseline": 100', captured["prompt"])
        self.assertTrue((self.repo / result["output_json"]).is_file())
        self.assertTrue((self.repo / result["output_markdown"]).is_file())

    def test_task_owned_workflow_evidence_is_injected_into_agent_prompt(self) -> None:
        collector = self.repo / "tasks" / "test-task" / "collect.py"
        collector.write_text("# test collector\n", encoding="utf-8")
        self.config["workflow"]["context_script"] = "tasks/test-task/collect.py"
        captured: Dict[str, str] = {}

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            captured["prompt"] = prompt
            return structured_result(SUCCESS_NO_NOTIFY)

        completed = CompletedProcess(
            ["python3", str(collector)],
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "indices": [{"name": "上证指数", "last": 3990.3}],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        with patch.object(production_runner.subprocess, "run", return_value=completed):
            result = execute_production_task(
                repo_root=self.repo,
                config=self.config,
                prompt_path=self.prompt_path,
                state_path=self.state_path,
                output_directory=self.output_directory,
                scheduled_at=self.scheduled_at,
                trigger_slot="09:20",
                agent_runner=agent,
            )
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertIn('"上证指数"', captured["prompt"])
        self.assertIn('"last": 3990.3', captured["prompt"])

    def test_skipped_non_trading_day_is_successful_noop_without_delivery(self) -> None:
        self.config["state"]["track_trigger_slots"] = True
        calls = []
        result = self.execute(
            structured_result(SKIPPED), sender=lambda *args, **kwargs: calls.append(1)
        )
        state = read_json_object(self.state_path)
        self.assertEqual(SKIPPED, result["status"])
        self.assertEqual([], calls)
        self.assertEqual(100, state["safe_baseline"])
        self.assertIn("2026-08-19|09:20", state["completed_trigger_slots"])

    def test_failed_or_invalid_result_cannot_pollute_domain_state(self) -> None:
        invalid = structured_result(FAILED, updates={"safe_baseline": None}, error="source down")
        result = self.execute(invalid)
        state = read_json_object(self.state_path)
        self.assertEqual(FAILED, result["status"])
        self.assertEqual(100, state["safe_baseline"])
        self.assertEqual(0, state["state_version"])
        self.assertNotIn("last_run_at", state)

    def test_reserved_state_property_gets_machine_readable_retry_then_safe_downgrade(self) -> None:
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["09:20"],
            }
        )
        self.config["state"]["allowed_update_keys"] = ["intraday_state"]
        prompts = []
        deliveries = []

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            prompts.append(prompt)
            return structured_result_v2(
                SUCCESS_NOTIFY,
                updates={
                    "intraday_state": {"2026-08-19|09:20": {"breadth": "verified"}},
                    "last_run_at": "agent-controlled-value",
                },
                event_key="market:2026-08-19:09:20",
                cards=market_dashboard_cards(),
            )

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
            delivery_sender=lambda text, **kwargs: (
                deliveries.append(kwargs)
                or {"status": "ok", "message_id": f"om_{len(deliveries)}"}
            ),
        )

        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(2, len(prompts))
        self.assertIn('"path": "/state_updates/1/namespace"', prompts[1])
        self.assertIn('"rule": "forbidden_property"', prompts[1])
        self.assertEqual(
            "verified",
            state["intraday_state"]["2026-08-19|09:20"]["breadth"],
        )
        self.assertNotEqual("agent-controlled-value", state["last_run_at"])
        self.assertEqual(3, len(deliveries))
        self.assertEqual(
            "/state_updates/1/namespace",
            result["validation_warnings"][0]["path"],
        )

    def test_unknown_state_property_is_not_safely_downgraded(self) -> None:
        self.config["state"]["allowed_update_keys"] = ["intraday_state"]
        prompts = []

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            prompts.append(prompt)
            return structured_result_v2(
                SUCCESS_NO_NOTIFY,
                updates={"intraday_state": {}, "invented_namespace": 1},
            )

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
        )

        state = read_json_object(self.state_path)
        self.assertEqual(FAILED, result["status"])
        self.assertEqual(2, len(prompts))
        self.assertIn(
            '"path": "/state_updates/1/namespace"', prompts[1]
        )
        self.assertNotIn("invented_namespace", state)
        self.assertEqual(0, state["state_version"])

    def test_invalid_state_value_json_gets_exact_correction_path(self) -> None:
        self.config["state"]["allowed_update_keys"] = ["intraday_state"]
        prompts = []

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            prompts.append(prompt)
            result = structured_result(
                SUCCESS_NO_NOTIFY,
                updates={"intraday_state": {"slot": {"breadth": "verified"}}},
            )
            if len(prompts) == 1:
                result["state_updates"][0]["value_json"] = "{"
            return result

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
        )

        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual(2, len(prompts))
        self.assertIn('"path": "/state_updates/0/value_json"', prompts[1])
        self.assertIn('"rule": "invalid_json"', prompts[1])
        self.assertEqual("verified", state["intraday_state"]["slot"]["breadth"])

    def test_delivery_is_sent_only_after_message_id_readback(self) -> None:
        self.config["delivery"]["readback"] = {
            "enabled": True,
            "trigger_slots": ["09:20"],
            "retry_attempts": 1,
        }
        reads = []
        with patch.object(
            production_runner,
            "read_message",
            side_effect=lambda message_id, **kwargs: (
                reads.append((message_id, kwargs))
                or {
                    "status": "ok",
                    "message_id": message_id,
                    "chat_id": "oc_market",
                    "msg_type": "post",
                }
            ),
            create=True,
        ):
            result = self.execute(
                structured_result_v2(
                    SUCCESS_NOTIFY,
                    event_key="market-readback:2026-08-19:09:20",
                ),
                sender=lambda text, **kwargs: {
                    "status": "ok",
                    "message_id": "om_verified",
                },
            )

        state = read_json_object(self.state_path)
        notifications = list(state["_runtime"]["notifications"].values())
        self.assertTrue(notifications, "delivery must create a pending notification")
        notification = notifications[0]
        message = notification["messages"][0]
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(["om_verified"], [value[0] for value in reads])
        self.assertEqual("verified", message["readback_status"])
        self.assertEqual("sent", message["status"])

    def test_readback_failure_keeps_pending_and_recovery_does_not_resend(self) -> None:
        self.config["delivery"]["readback"] = {
            "enabled": True,
            "trigger_slots": ["09:20"],
            "retry_attempts": 1,
        }
        sends = []
        with patch.object(
            production_runner,
            "read_message",
            side_effect=FeishuDeliveryError("message not visible yet"),
            create=True,
        ):
            first = self.execute(
                structured_result_v2(
                    SUCCESS_NOTIFY,
                    event_key="market-readback-pending:2026-08-19:09:20",
                ),
                sender=lambda text, **kwargs: (
                    sends.append(kwargs)
                    or {"status": "ok", "message_id": "om_pending_readback"}
                ),
            )

        state = read_json_object(self.state_path)
        notifications = list(state["_runtime"]["notifications"].values())
        self.assertTrue(notifications, "delivery must create a pending notification")
        notification = notifications[0]
        self.assertEqual(FAILED, first["status"])
        self.assertEqual("pending", notification["status"])
        self.assertEqual("om_pending_readback", notification["messages"][0]["message_id"])

        with patch.object(
            production_runner,
            "read_message",
            return_value={
                "status": "ok",
                "message_id": "om_pending_readback",
                "chat_id": "oc_market",
                "msg_type": "post",
            },
            create=True,
        ):
            recovery = recover_pending_delivery(
                config=self.config,
                state_path=self.state_path,
                delivery_sender=lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("accepted message must not be sent twice")
                ),
            )

        updated = read_json_object(self.state_path)
        recovered_notification = next(
            iter(updated["_runtime"]["notifications"].values())
        )
        self.assertEqual(SUCCESS_NO_NOTIFY, recovery["status"])
        self.assertEqual(1, recovery["recovered"])
        self.assertEqual(1, len(sends))
        self.assertEqual("sent", recovered_notification["status"])


    def test_failed_data_only_run_sends_one_deterministic_failure_alert(self) -> None:
        self.config["schedule"]["triggers"] = ["09:20", "11:20"]
        self.config["delivery"]["notification_triggers"] = ["11:20"]
        self.config["delivery"]["chat_id_env"] = (
            "FEISHU_CHAT_ID_TEST_TASK_SCHEDULE_TASK"
        )
        self.config["delivery"]["failure_alert"] = {
            "enabled": True,
            "trigger_slots": ["09:20", "11:20"],
        }
        deliveries = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            deliveries.append({"text": text, **kwargs})
            return {"status": "ok", "message_id": "om_failure_alert"}

        result = self.execute(
            structured_result(FAILED, error="market collector timed out"),
            sender=sender,
            trigger_slot="09:20",
        )

        state = read_json_object(self.state_path)
        alerts = [
            value
            for value in state["_runtime"]["notifications"].values()
            if value.get("purpose") == "failure_alert"
        ]
        self.assertEqual(FAILED, result["status"])
        self.assertEqual("sent", result["failure_alert_status"])
        self.assertEqual(1, len(deliveries))
        self.assertEqual("post", deliveries[0]["message_type"])
        self.assertEqual(
            "FEISHU_CHAT_ID_TEST_TASK_SCHEDULE_TASK",
            deliveries[0]["chat_id_env"],
        )
        self.assertIn("09:20", deliveries[0]["title"])
        self.assertIn("market collector timed out", deliveries[0]["text"])
        self.assertIn(result["run_id"], deliveries[0]["text"])
        self.assertEqual(1, len(alerts))
        self.assertEqual("sent", alerts[0]["status"])
        health_records = [
            json.loads(line)
            for line in (self.repo / "logs" / "scheduler" / "health.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual("run_failed", health_records[-1]["reason"])
        self.assertEqual(result["run_id"], health_records[-1]["run_id"])
        self.assertEqual("sent", health_records[-1]["alert_status"])

    def test_main_delivery_failure_sends_failure_alert_and_keeps_main_pending(self) -> None:
        self.config["delivery"]["failure_alert"] = {
            "enabled": True,
            "trigger_slots": ["09:20"],
        }
        deliveries = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            deliveries.append({"text": text, **kwargs})
            if kwargs.get("title") == "Important update":
                raise FeishuDeliveryError("market card delivery unavailable")
            return {"status": "ok", "message_id": "om_delivery_failure_alert"}

        with patch.object(production_runner.time, "sleep", return_value=None):
            result = self.execute(
                structured_result(
                    SUCCESS_NOTIFY,
                    event_key="market:2026-08-19:09:20",
                ),
                sender=sender,
            )

        state = read_json_object(self.state_path)
        notifications = list(state["_runtime"]["notifications"].values())
        normal = [value for value in notifications if not value.get("purpose")]
        alerts = [
            value
            for value in notifications
            if value.get("purpose") == "failure_alert"
        ]
        self.assertEqual(FAILED, result["status"])
        self.assertEqual("failed", result["delivery_status"])
        self.assertEqual("sent", result["failure_alert_status"])
        self.assertEqual("pending", normal[0]["status"])
        self.assertEqual("sent", alerts[0]["status"])
        self.assertIn("market card delivery unavailable", alerts[0]["body"])

    def test_failed_failure_alert_stays_pending_and_recovers_once(self) -> None:
        self.config["delivery"]["notification_triggers"] = ["11:20"]
        self.config["delivery"]["failure_alert"] = {
            "enabled": True,
            "trigger_slots": ["09:20"],
        }

        def failing_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            raise FeishuDeliveryError("alert channel temporarily unavailable")

        with patch.object(production_runner.time, "sleep", return_value=None):
            result = self.execute(
                structured_result(FAILED, error="sampling failed"),
                sender=failing_sender,
            )

        state = read_json_object(self.state_path)
        alerts = [
            value
            for value in state["_runtime"]["notifications"].values()
            if value.get("purpose") == "failure_alert"
        ]
        self.assertEqual(FAILED, result["status"])
        self.assertEqual("pending", result["failure_alert_status"])
        self.assertEqual(1, len(alerts))
        self.assertEqual("pending", alerts[0]["status"])

        recovered = []
        recovery = recover_pending_delivery(
            config=self.config,
            state_path=self.state_path,
            delivery_sender=lambda text, **kwargs: (
                recovered.append(kwargs["idempotency_key"])
                or {"status": "ok", "message_id": "om_recovered_alert"}
            ),
        )

        updated = read_json_object(self.state_path)
        updated_alerts = [
            value
            for value in updated["_runtime"]["notifications"].values()
            if value.get("purpose") == "failure_alert"
        ]
        self.assertEqual(SUCCESS_NO_NOTIFY, recovery["status"])
        self.assertEqual(1, recovery["recovered"])
        self.assertEqual(1, len(recovered))
        self.assertEqual("sent", updated_alerts[0]["status"])

    def test_harness_removes_reserved_runtime_update_after_retry(self) -> None:
        result = self.execute(
            structured_result(
                SUCCESS_NO_NOTIFY,
                updates={"_runtime": {"notifications": {}}},
            )
        )

        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual(1, state["state_version"])
        self.assertEqual("/state_updates/0/namespace", result["validation_warnings"][0]["path"])
        self.assertEqual({}, state["_runtime"]["notifications"])
        self.assertNotEqual({}, state["_runtime"]["processed_runs"])

    def test_notification_idempotency_dedup_and_duplicate_run(self) -> None:
        calls = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(kwargs)
            return {"status": "ok", "message_id": "om_1"}

        payload = structured_result(SUCCESS_NOTIFY, event_key="paper:123:v2")
        first = self.execute(payload, sender=sender)
        duplicate_run = self.execute(payload, sender=sender)
        later = self.execute(
            payload,
            sender=sender,
            scheduled_at=self.scheduled_at.replace(hour=10),
            trigger_slot="10:00",
        )
        self.assertEqual(SUCCESS_NOTIFY, first["status"])
        self.assertTrue(first["notification_sent"])
        self.assertTrue(duplicate_run["duplicate_run"])
        self.assertEqual(SUCCESS_NO_NOTIFY, later["status"])
        self.assertEqual("duplicate_suppressed", later["delivery_status"])
        self.assertEqual(1, len(calls))
        self.assertLessEqual(len(calls[0]["idempotency_key"]), 50)

    def test_delivery_failure_is_recoverable_without_rerunning_agent(self) -> None:
        agent_calls = []

        def failing_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            raise FeishuDeliveryError("temporary delivery failure")

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            agent_calls.append(1)
            return structured_result(SUCCESS_NOTIFY, event_key="price:sku:drop")

        with patch.object(production_runner.time, "sleep", return_value=None):
            first = execute_production_task(
                repo_root=self.repo,
                config=self.config,
                prompt_path=self.prompt_path,
                state_path=self.state_path,
                output_directory=self.output_directory,
                scheduled_at=self.scheduled_at,
                trigger_slot="09:20",
                agent_runner=agent,
                delivery_sender=failing_sender,
            )
        self.assertEqual(FAILED, first["status"])
        self.assertEqual(1, len(agent_calls))

        sends = []

        def successful_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            sends.append(kwargs["idempotency_key"])
            return {"status": "ok", "message_id": "om_recovered"}

        recovery = recover_pending_delivery(
            config=self.config,
            state_path=self.state_path,
            delivery_sender=successful_sender,
        )
        self.assertEqual(SUCCESS_NO_NOTIFY, recovery["status"])
        self.assertEqual(1, recovery["recovered"])
        self.assertEqual(1, len(sends))
        self.assertEqual(1, len(agent_calls))

    def test_delivery_response_requires_nonempty_message_id(self) -> None:
        result = self.execute(
            structured_result(SUCCESS_NOTIFY, event_key="delivery:missing-id"),
            sender=lambda text, **kwargs: {"status": "ok"},
        )

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(FAILED, result["status"])
        self.assertEqual("failed", result["delivery_status"])
        self.assertEqual("pending", notification["status"])
        self.assertEqual("pending", notification["messages"][0]["status"])
        self.assertIn("message_id", notification["messages"][0]["last_error"])

    def test_task_chat_route_is_forwarded_to_delivery_adapter(self) -> None:
        self.config["delivery"]["chat_id_env"] = (
            "FEISHU_CHAT_ID_TEST_TASK_SCHEDULE_TASK"
        )
        calls = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(kwargs)
            return {"status": "ok", "message_id": "om_routed"}

        result = self.execute(
            structured_result(SUCCESS_NOTIFY, event_key="route:test"),
            sender=sender,
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(
            "FEISHU_CHAT_ID_TEST_TASK_SCHEDULE_TASK",
            calls[0]["chat_id_env"],
        )

    def test_data_only_trigger_suppresses_notification_but_keeps_state(self) -> None:
        self.config["delivery"]["notification_triggers"] = [
            "09:35",
            "11:20",
            "15:01",
        ]
        calls = []
        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="must-not-send",
                updates={
                    "intraday_state": {
                        "2026-08-19|09:45": {"breadth": "verified"}
                    }
                },
            ),
            sender=lambda *args, **kwargs: calls.append(kwargs),
            trigger_slot="09:45",
        )
        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual("not_allowed_for_trigger", result["delivery_status"])
        self.assertEqual([], calls)
        self.assertEqual(
            {"breadth": "verified"},
            state["intraday_state"]["2026-08-19|09:45"],
        )
        self.assertEqual({}, state["_runtime"]["notifications"])

    def test_notification_trigger_requires_success_notify(self) -> None:
        self.config["delivery"]["notification_triggers"] = [
            "09:35",
            "11:20",
            "15:01",
        ]
        result = self.execute(
            structured_result(SUCCESS_NO_NOTIFY),
            trigger_slot="09:35",
        )
        self.assertEqual(FAILED, result["status"])
        self.assertIn("requires SUCCESS_NOTIFY", result["error"])

    def test_opening_trigger_allows_fake_delivery(self) -> None:
        self.config["delivery"]["notification_triggers"] = [
            "09:35",
            "11:20",
            "15:01",
        ]
        calls = []
        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="market:2026-08-20:09:35",
            ),
            sender=lambda text, **kwargs: (
                calls.append(kwargs)
                or {"status": "ok", "message_id": "om_fake_opening"}
            ),
            trigger_slot="09:35",
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(1, len(calls))

    def test_closing_review_is_pinned_after_all_original_cards_are_sent(self) -> None:
        self.config["schedule"]["triggers"] = ["15:01"]
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["15:01"],
                "daily_archive": {
                    "enabled": True,
                    "trigger_slots": ["15:01"],
                },
            }
        )
        events = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            message_id = f"om_close_{len(events) + 1}"
            events.append(f"send:{message_id}")
            return {"status": "ok", "message_id": message_id}

        def pinner(message_id: str) -> Dict[str, Any]:
            events.append(f"pin:{message_id}")
            return {
                "status": "ok",
                "message_id": message_id,
                "chat_id": "oc_market",
                "create_time": "1787410860000",
            }

        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="market:2026-08-19:15:01",
                cards=market_dashboard_cards(),
            ),
            sender=sender,
            pin_sender=pinner,
            scheduled_at=self.scheduled_at.replace(hour=15, minute=1),
            trigger_slot="15:01",
        )

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(
            [
                "send:om_close_1",
                "send:om_close_2",
                "send:om_close_3",
                "pin:om_close_1",
            ],
            events,
        )
        archive = notification["daily_archive"]
        self.assertEqual("pinned", archive["status"])
        self.assertEqual("om_close_1", archive["message_id"])
        self.assertIsInstance(archive["pinned_at"], str)
        self.assertIsNone(archive["last_error"])

    def test_non_closing_review_does_not_create_a_daily_archive(self) -> None:
        self.config["schedule"]["triggers"] = ["11:20", "15:01"]
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["11:20", "15:01"],
                "daily_archive": {
                    "enabled": True,
                    "trigger_slots": ["15:01"],
                },
            }
        )
        sends = []

        def must_not_pin(message_id: str) -> Dict[str, Any]:
            raise AssertionError(f"unexpected Pin for {message_id}")

        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="market:2026-08-19:11:20",
                cards=market_dashboard_cards(),
            ),
            sender=lambda text, **kwargs: (
                sends.append(kwargs)
                or {"status": "ok", "message_id": f"om_midday_{len(sends)}"}
            ),
            pin_sender=must_not_pin,
            scheduled_at=self.scheduled_at.replace(hour=11, minute=20),
            trigger_slot="11:20",
        )

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(3, len(sends))
        self.assertNotIn("daily_archive", notification)

    def test_daily_archive_failure_never_breaks_original_card_delivery(self) -> None:
        self.config["schedule"]["triggers"] = ["15:01"]
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["15:01"],
                "daily_archive": {
                    "enabled": True,
                    "trigger_slots": ["15:01"],
                },
            }
        )
        sends = []

        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="market:2026-08-19:15:01",
                cards=market_dashboard_cards(),
            ),
            sender=lambda text, **kwargs: (
                sends.append(kwargs)
                or {"status": "ok", "message_id": f"om_close_{len(sends)}"}
            ),
            pin_sender=lambda message_id: (_ for _ in ()).throw(
                FeishuDeliveryError("missing Pin permission")
            ),
            scheduled_at=self.scheduled_at.replace(hour=15, minute=1),
            trigger_slot="15:01",
        )

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual("ok", result["delivery_status"])
        self.assertEqual(3, len(sends))
        self.assertEqual("sent", notification["status"])
        archive = notification.get("daily_archive")
        self.assertIsNotNone(archive)
        self.assertEqual("failed", archive["status"])
        self.assertIn(
            "missing Pin permission", archive["last_error"]
        )

    def test_unexpected_daily_archive_error_never_breaks_original_delivery(self) -> None:
        self.config["schedule"]["triggers"] = ["15:01"]
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["15:01"],
                "daily_archive": {
                    "enabled": True,
                    "trigger_slots": ["15:01"],
                },
            }
        )
        sends = []

        try:
            result = self.execute(
                structured_result(
                    SUCCESS_NOTIFY,
                    event_key="market:2026-08-19:15:01:unexpected-pin-error",
                    cards=market_dashboard_cards(),
                ),
                sender=lambda text, **kwargs: (
                    sends.append(kwargs)
                    or {"status": "ok", "message_id": f"om_close_{len(sends)}"}
                ),
                pin_sender=lambda message_id: (_ for _ in ()).throw(
                    RuntimeError("unexpected Pin adapter failure")
                ),
                scheduled_at=self.scheduled_at.replace(hour=15, minute=1),
                trigger_slot="15:01",
            )
        except RuntimeError as exc:
            self.fail(f"optional daily archive error escaped delivery: {exc}")

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(3, len(sends))
        self.assertEqual("sent", notification["status"])
        self.assertEqual("failed", notification["daily_archive"]["status"])
        self.assertIn(
            "unexpected Pin adapter failure",
            notification["daily_archive"]["last_error"],
        )

    def test_partial_closing_delivery_pins_only_after_recovery_completes(self) -> None:
        self.config["schedule"]["triggers"] = ["15:01"]
        self.config["delivery"].update(
            {
                "presentation": "market_dashboard_card",
                "notification_triggers": ["15:01"],
                "daily_archive": {
                    "enabled": True,
                    "trigger_slots": ["15:01"],
                },
            }
        )
        initial_sends = []
        pins = []

        def partial_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            initial_sends.append(kwargs["idempotency_key"])
            if len(initial_sends) == 3:
                raise FeishuDeliveryError("third closing card failed")
            return {
                "status": "ok",
                "message_id": f"om_close_{len(initial_sends)}",
            }

        first = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="market:2026-08-19:15:01:partial",
                cards=market_dashboard_cards(),
            ),
            sender=partial_sender,
            pin_sender=lambda message_id: (
                pins.append(message_id)
                or {"status": "ok", "message_id": message_id}
            ),
            scheduled_at=self.scheduled_at.replace(hour=15, minute=1),
            trigger_slot="15:01",
        )

        self.assertEqual(FAILED, first["status"])
        self.assertEqual([], pins)

        recovery_sends = []
        recovery = recover_pending_delivery(
            config=self.config,
            state_path=self.state_path,
            delivery_sender=lambda text, **kwargs: (
                recovery_sends.append(kwargs["idempotency_key"])
                or {"status": "ok", "message_id": "om_close_3"}
            ),
            pin_sender=lambda message_id: (
                pins.append(message_id)
                or {"status": "ok", "message_id": message_id}
            ),
        )

        state = read_json_object(self.state_path)
        notification = next(iter(state["_runtime"]["notifications"].values()))
        self.assertEqual(SUCCESS_NO_NOTIFY, recovery["status"])
        self.assertEqual(1, recovery["recovered"])
        self.assertEqual(1, len(recovery_sends))
        self.assertEqual(["om_close_1"], pins)
        self.assertEqual("sent", notification["status"])
        self.assertEqual(
            ["om_close_1", "om_close_2", "om_close_3"],
            notification["message_ids"],
        )
        self.assertEqual("pinned", notification["daily_archive"]["status"])

    def test_recovery_suppresses_pending_notification_from_data_only_slot(self) -> None:
        self.config["delivery"]["notification_triggers"] = [
            "09:35",
            "11:20",
            "15:01",
        ]
        state = read_json_object(self.state_path)
        state["_runtime"]["processed_runs"]["old-run"] = {
            "trigger_slot": "14:30",
            "terminal": True,
        }
        state["_runtime"]["notifications"]["old-fingerprint"] = {
            "status": "pending",
            "run_id": "old-run",
            "event_key": "old-event",
            "messages": [],
        }
        atomic_write_json(self.state_path, state)
        calls = []
        recovery = recover_pending_delivery(
            config=self.config,
            state_path=self.state_path,
            delivery_sender=lambda *args, **kwargs: calls.append(kwargs),
        )
        updated = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, recovery["status"])
        self.assertEqual(0, recovery["failed"])
        self.assertEqual([], calls)
        self.assertEqual(
            "suppressed_by_trigger_policy",
            updated["_runtime"]["notifications"]["old-fingerprint"]["status"],
        )

    def test_terminal_run_identity_survives_restart(self) -> None:
        result = self.execute(structured_result(SUCCESS_NO_NOTIFY))
        expected_run_id = make_run_id(
            "test-task", self.scheduled_at.isoformat(timespec="seconds"), "09:20"
        )
        self.assertEqual(expected_run_id, result["run_id"])
        state = read_json_object(self.state_path)
        self.assertTrue(state["_runtime"]["processed_runs"][expected_run_id]["terminal"])

    def test_dead_process_lock_is_recovered_after_restart(self) -> None:
        lock_path = self.repo / "state" / ".locks" / "test-task.lock"
        atomic_write_json(
            lock_path,
            {"pid": 99999999, "created_at": "2026-08-19T09:00:00+08:00"},
        )
        with run_lock(lock_path, stale_seconds=3600):
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

    def test_research_top5_sends_exactly_five_card_messages(self) -> None:
        self.config["delivery"]["presentation"] = "research_top5_cards"
        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        calls = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(kwargs)
            return {"status": "ok", "message_id": f"om_{len(calls)}"}

        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="research:daily:2026-08-19",
                cards=cards,
            ),
            sender=sender,
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(5, len(calls))
        self.assertEqual({"interactive"}, {call["message_type"] for call in calls})
        self.assertEqual(5, len({call["idempotency_key"] for call in calls}))
        for call in calls:
            self.assertEqual("2.0", call["card"]["schema"])
            self.assertLessEqual(len(call["card"]["body"]["elements"]), 5)

    def test_wrong_research_card_count_is_failed_before_delivery(self) -> None:
        self.config["delivery"]["presentation"] = "research_top5_cards"
        cards = [semantic_card("research_item", rank=index) for index in range(1, 5)]
        calls = []
        result = self.execute(
            structured_result(SUCCESS_NOTIFY, event_key="too-few", cards=cards),
            sender=lambda *args, **kwargs: calls.append(1),
        )
        state = read_json_object(self.state_path)
        self.assertEqual(FAILED, result["status"])
        self.assertEqual([], calls)
        self.assertEqual(0, state["state_version"])

    def test_invalid_card_shape_gets_one_semantic_correction_retry(self) -> None:
        self.config["delivery"]["presentation"] = "research_top5_cards"
        attempts = []
        deliveries = []

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            attempts.append(prompt)
            count = 4 if len(attempts) == 1 else 5
            cards = [
                semantic_card("research_item", rank=index)
                for index in range(1, count + 1)
            ]
            return structured_result(
                SUCCESS_NOTIFY,
                event_key="research:corrected",
                cards=cards,
            )

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
            delivery_sender=lambda text, **kwargs: (
                deliveries.append(kwargs)
                or {"status": "ok", "message_id": f"om_{len(deliveries)}"}
            ),
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(2, len(attempts))
        self.assertIn("requires 5 cards", attempts[1])
        self.assertEqual(5, len(deliveries))

    def test_always_delivery_policy_retries_silent_success(self) -> None:
        self.config["delivery"]["policy"] = "always"
        self.config["delivery"]["presentation"] = "price_alert_cards"
        attempts = []
        deliveries = []

        def agent(prompt: str, config: Dict[str, Any], root: Path) -> Dict[str, Any]:
            attempts.append(prompt)
            if len(attempts) == 1:
                return structured_result(SUCCESS_NO_NOTIFY)
            return structured_result(
                SUCCESS_NOTIFY,
                event_key="price:daily-status:2026-08-19",
                cards=[semantic_card("price_alert")],
            )

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=agent,
            delivery_sender=lambda text, **kwargs: (
                deliveries.append(kwargs)
                or {"status": "ok", "message_id": "om_daily_status"}
            ),
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(2, len(attempts))
        self.assertIn("delivery.policy=always", attempts[1])
        self.assertEqual(1, len(deliveries))

    def test_always_policy_delivers_same_event_on_each_scheduled_run(self) -> None:
        self.config["delivery"]["policy"] = "always"
        self.config["delivery"]["presentation"] = "price_alert_cards"
        deliveries = []
        payload = structured_result(
            SUCCESS_NOTIFY,
            event_key="daily-briefing:same-ranked-items",
            cards=[semantic_card("price_alert")],
        )

        first = self.execute(
            payload,
            sender=lambda text, **kwargs: (
                deliveries.append(kwargs)
                or {"status": "ok", "message_id": "om_day_1"}
            ),
        )
        second = self.execute(
            payload,
            sender=lambda text, **kwargs: (
                deliveries.append(kwargs)
                or {"status": "ok", "message_id": "om_day_2"}
            ),
            scheduled_at=self.scheduled_at.replace(day=20),
        )

        self.assertEqual(SUCCESS_NOTIFY, first["status"])
        self.assertEqual(SUCCESS_NOTIFY, second["status"])
        self.assertTrue(first["notification_sent"])
        self.assertTrue(second["notification_sent"])
        self.assertEqual(2, len(deliveries))

    def test_partial_five_card_delivery_recovers_remaining_cards_only(self) -> None:
        self.config["delivery"]["presentation"] = "research_top5_cards"
        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        initial_calls = []

        def partial_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            initial_calls.append(kwargs["idempotency_key"])
            if len(initial_calls) == 3:
                raise FeishuDeliveryError("third card failed")
            return {"status": "ok", "message_id": f"om_{len(initial_calls)}"}

        first = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="research:partial",
                cards=cards,
            ),
            sender=partial_sender,
        )
        self.assertEqual(FAILED, first["status"])
        self.assertEqual(3, len(initial_calls))

        recovered_calls = []

        def recovery_sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            recovered_calls.append(kwargs["idempotency_key"])
            return {"status": "ok", "message_id": f"om_r{len(recovered_calls)}"}

        recovery = recover_pending_delivery(
            config=self.config,
            state_path=self.state_path,
            delivery_sender=recovery_sender,
        )
        self.assertEqual(1, recovery["recovered"])
        self.assertEqual(3, len(recovered_calls))
        self.assertNotIn(initial_calls[0], recovered_calls)
        self.assertNotIn(initial_calls[1], recovered_calls)

    def test_optional_card_image_failure_degrades_to_card_without_image(self) -> None:
        self.config["delivery"]["presentation"] = "price_alert_cards"
        card = semantic_card(
            "price_alert", rank=1, image_url="https://example.com/product.png"
        )
        delivered = []

        def sender(text: str, **kwargs: Any) -> Dict[str, Any]:
            delivered.append(kwargs["card"])
            return {"status": "ok", "message_id": "om_no_image"}

        result = execute_production_task(
            repo_root=self.repo,
            config=self.config,
            prompt_path=self.prompt_path,
            state_path=self.state_path,
            output_directory=self.output_directory,
            scheduled_at=self.scheduled_at,
            trigger_slot="09:20",
            agent_runner=lambda prompt, config, root: structured_result(
                SUCCESS_NOTIFY,
                event_key="price:image-fallback",
                cards=[card],
            ),
            delivery_sender=sender,
            image_uploader=lambda url: (_ for _ in ()).throw(
                FeishuDeliveryError("missing image scope")
            ),
        )
        self.assertEqual(SUCCESS_NOTIFY, result["status"])
        self.assertEqual(1, len(delivered))
        self.assertNotIn("img", {element["tag"] for element in delivered[0]["body"]["elements"]})


class CardRenderingTests(unittest.TestCase):
    def test_research_cards_show_overview_before_collapsed_abstract_and_details(self) -> None:
        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        try:
            cards = feishu_cards.validate_card_specs(cards)
        except feishu_cards.CardSpecError as exc:
            self.fail(f"new research card contract was rejected: {exc}")
        feishu_cards.validate_presentation(
            "research_top5_cards", cards, should_notify=True
        )

        rendered = feishu_cards.render_card(cards[0])
        self.assertEqual(
            "Memory-R1: Learning to Manage Agent Memory",
            rendered["header"]["title"]["content"],
        )
        self.assertEqual(
            "Memory-R1：学习管理智能体记忆",
            rendered["header"]["subtitle"]["content"],
        )
        self.assertIn("一句话概述", rendered["body"]["elements"][0]["content"])
        panel = rendered["body"]["elements"][2]
        self.assertEqual(5, len(panel["elements"]))
        self.assertIn("论文摘要翻译", panel["elements"][0]["content"])

        cards[0]["sections"][1]["content"] = "English abstract only."
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "substantive Chinese abstract translation"
        ):
            feishu_cards.validate_presentation(
                "research_top5_cards", cards, should_notify=True
            )

        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        cards[0]["sections"][1], cards[0]["sections"][2] = (
            cards[0]["sections"][2],
            cards[0]["sections"][1],
        )
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "sections must be ordered"
        ):
            feishu_cards.validate_presentation(
                "research_top5_cards", cards, should_notify=True
            )

    def test_research_overview_rejects_experimental_numbers(self) -> None:
        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        cards[0]["sections"][0]["content"] = (
            "该研究解决记忆检索失配问题，并把任务成功率提升到百分之八十。"
        )

        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "must omit experimental data"
        ):
            feishu_cards.validate_presentation(
                "research_top5_cards", cards, should_notify=True
            )

    def test_research_cards_require_visible_summary_and_research_metadata(self) -> None:
        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        cards[0]["sections"][0]["collapsed"] = True
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "one visible overview"
        ):
            feishu_cards.validate_presentation(
                "research_top5_cards", cards, should_notify=True
            )

        cards = [semantic_card("research_item", rank=index) for index in range(1, 6)]
        cards[0]["fields"] = cards[0]["fields"][:-1]
        with self.assertRaisesRegex(feishu_cards.CardSpecError, "missing fields"):
            feishu_cards.validate_presentation(
                "research_top5_cards", cards, should_notify=True
            )

    def test_card_2_structure_has_one_focus_and_grouped_details(self) -> None:
        card = feishu_cards.render_card(semantic_card("research_item", rank=1))
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("default", card["config"]["width_mode"])
        tags = [element["tag"] for element in card["body"]["elements"]]
        self.assertEqual(
            ["markdown", "column_set", "collapsible_panel", "button"], tags
        )
        focus_markdown = card["body"]["elements"][1]["columns"][0]["elements"][0]
        self.assertTrue(focus_markdown["content"].startswith("## "))
        focus_column = card["body"]["elements"][1]["columns"][0]
        self.assertNotIn("corner_radius", focus_column)
        button = card["body"]["elements"][-1]
        self.assertEqual("open_url", button["behaviors"][0]["type"])
        panel = card["body"]["elements"][2]
        self.assertFalse(panel["expanded"])
        self.assertEqual(
            "论文详解（点击展开/收起）", panel["header"]["title"]["content"]
        )
        self.assertIn("一句话概述", card["body"]["elements"][0]["content"])
        self.assertEqual(5, len(panel["elements"]))
        for element, title in zip(
            panel["elements"], feishu_cards.RESEARCH_SECTION_TITLES[1:]
        ):
            self.assertIn(title, element["content"])

    def test_market_card_requires_hidden_detail_section(self) -> None:
        cards = market_dashboard_cards()
        cards[0]["sections"] = [
            {"title": "Summary", "content": "Only a summary.", "collapsed": False}
        ]
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "requires one visible and two collapsed sections"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

    def test_three_market_cards_enforce_progressive_roles_colors_and_source_tail(self) -> None:
        cards = market_dashboard_cards()
        feishu_cards.validate_presentation(
            "market_dashboard_card", cards, should_notify=True
        )
        for card in cards:
            rendered = feishu_cards.render_card(card)
            self.assertEqual(
                ["markdown", "column_set", "collapsible_panel", "button"],
                [element["tag"] for element in rendered["body"]["elements"]],
            )
            self.assertIn(
                card["sections"][0]["title"],
                rendered["body"]["elements"][0]["content"],
            )
            self.assertEqual("button", rendered["body"]["elements"][-1]["tag"])
            self.assertEqual(
                "open_url",
                rendered["body"]["elements"][-1]["behaviors"][0]["type"],
            )
            panels = [
                element
                for element in rendered["body"]["elements"]
                if element["tag"] == "collapsible_panel"
            ]
            self.assertEqual(1, len(panels))
            self.assertFalse(panels[0]["expanded"])
            self.assertEqual(
                "证据详情（点击展开/收起）",
                panels[0]["header"]["title"]["content"],
            )

        cards[0]["theme"] = "green"
        cards[1]["theme"] = "red"
        feishu_cards.validate_presentation(
            "market_dashboard_card", cards, should_notify=True
        )

        cards[0]["theme"] = "orange"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "盘面总览 theme must be one of"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

        cards = market_dashboard_cards()
        cards[1]["theme"] = "blue"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "情绪与主线 theme must be one of"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

        cards = market_dashboard_cards()
        cards[2]["theme"] = "red"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "异动与风险 theme must be one of: yellow"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

        cards = market_dashboard_cards()
        cards[1]["sections"][1]["title"] = "主线详情"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "sections must be ordered as"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

    def test_card_renderer_rejects_undocumented_component_fields(self) -> None:
        card = feishu_cards.render_card(semantic_card("research_item", rank=1))
        column_set = next(
            element
            for element in card["body"]["elements"]
            if element["tag"] == "column_set"
        )
        column_set["columns"][0]["corner_radius"] = "8px"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "unsupported column fields: corner_radius"
        ):
            feishu_cards.validate_rendered_card(card)

    def test_card_renderer_escapes_agent_markup(self) -> None:
        spec = semantic_card("research_item", rank=1)
        spec["sections"][0]["content"] = "<at id=all></at> **unsafe**"
        card = feishu_cards.render_card(spec)
        content = card["body"]["elements"][0]["content"]
        self.assertNotIn("<at", content)
        self.assertIn("&lt;at", content)

    def test_private_image_url_is_rejected(self) -> None:
        with self.assertRaises(FeishuDeliveryError):
            feishu_send._assert_public_https_url("https://127.0.0.1/image.png")


class AdapterAndRetryTests(unittest.TestCase):
    def test_feishu_readback_confirms_message_in_expected_task_chat(self) -> None:
        reader = getattr(feishu_send, "read_message", None)
        self.assertIsNotNone(reader, "Feishu adapter must expose message readback")
        if reader is None:
            return
        task_chat_env = "FEISHU_CHAT_ID_A_SHARE_MONITOR_SCHEDULE_TASK"
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "message_id": "om_verified",
                            "chat_id": "a-share-chat-id",
                            "msg_type": "interactive",
                        }
                    ]
                },
            },
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "shared-app-id",
            feishu_send.APP_SECRET_ENV_KEY: "shared-app-secret",
            task_chat_env: "a-share-chat-id",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            feishu_send, "_request_get_json", side_effect=responses[1:]
        ) as request_get, patch.object(
            feishu_send, "_request_json", return_value=responses[0]
        ):
            result = reader("om_verified", chat_id_env=task_chat_env)

        self.assertEqual("om_verified", result["message_id"])
        self.assertEqual("a-share-chat-id", result["chat_id"])
        self.assertEqual(
            "https://open.feishu.cn/open-apis/im/v1/messages/om_verified",
            request_get.call_args.args[0],
        )

    def test_feishu_adapter_pins_a_sent_message(self) -> None:
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {
                "code": 0,
                "data": {
                    "pin": {
                        "message_id": "om_daily_close",
                        "chat_id": "oc_market",
                        "operator_id": "cli_app",
                        "operator_id_type": "app_id",
                        "create_time": "1787410860000",
                    }
                },
            },
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: "app-secret",
        }
        pin_operation = getattr(feishu_send, "pin_message", lambda message_id: None)
        with patch.dict(os.environ, env, clear=False), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ) as request:
            result = pin_operation("om_daily_close")

        self.assertIsNotNone(result)
        self.assertEqual("om_daily_close", result["message_id"])
        self.assertEqual(
            "https://open.feishu.cn/open-apis/im/v1/pins",
            request.call_args_list[1].args[0],
        )
        self.assertEqual(
            {"message_id": "om_daily_close"},
            request.call_args_list[1].args[1],
        )

    def test_feishu_pin_adapter_rejects_success_without_message_id(self) -> None:
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"pin": {"chat_id": "oc_market"}}},
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: "app-secret",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ):
            with self.assertRaisesRegex(
                FeishuDeliveryError, "returned no message_id"
            ):
                feishu_send.pin_message("om_daily_close")

    def test_feishu_pin_error_redacts_configured_secret(self) -> None:
        secret = "local-app-secret-value"
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 999, "msg": f"permission denied for {secret}"},
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: secret,
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ):
            with self.assertRaises(FeishuDeliveryError) as raised:
                feishu_send.pin_message("om_daily_close")

        error = str(raised.exception)
        self.assertNotIn(secret, error)
        self.assertIn("[REDACTED]", error)

    def test_feishu_adapter_forwards_uuid_without_real_network(self) -> None:
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"message_id": "om_mock"}},
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: "app-secret",
            feishu_send.CHAT_ID_ENV_KEY: "chat-id",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ) as request:
            result = feishu_send.send_message(
                "hello",
                message_type="post",
                title="title",
                idempotency_key="stable-uuid",
            )
        self.assertEqual("om_mock", result["message_id"])
        message_payload = request.call_args_list[1].args[1]
        self.assertEqual("stable-uuid", message_payload["uuid"])
        self.assertEqual("chat-id", message_payload["receive_id"])

    def test_feishu_adapter_sends_interactive_card_payload(self) -> None:
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"message_id": "om_card"}},
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: "app-secret",
            feishu_send.CHAT_ID_ENV_KEY: "chat-id",
        }
        card = feishu_cards.render_card(semantic_card("market_dashboard"))
        with patch.dict(os.environ, env, clear=False), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ) as request:
            result = feishu_send.send_message(
                "",
                message_type="interactive",
                card=card,
                idempotency_key="card-uuid",
            )
        self.assertEqual("om_card", result["message_id"])
        message_payload = request.call_args_list[1].args[1]
        self.assertEqual("interactive", message_payload["msg_type"])
        self.assertEqual("2.0", json.loads(message_payload["content"])["schema"])

    def test_feishu_adapter_uses_task_specific_chat_without_changing_bot(self) -> None:
        task_chat_env = "FEISHU_CHAT_ID_A_SHARE_MONITOR_SCHEDULE_TASK"
        responses = [
            {"code": 0, "tenant_access_token": "tenant-token"},
            {"code": 0, "data": {"message_id": "om_task_chat"}},
        ]
        env = {
            feishu_send.APP_ID_ENV_KEY: "shared-app-id",
            feishu_send.APP_SECRET_ENV_KEY: "shared-app-secret",
            feishu_send.CHAT_ID_ENV_KEY: "default-chat-id",
            task_chat_env: "a-share-chat-id",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            feishu_send, "_request_json", side_effect=responses
        ) as request:
            result = feishu_send.send_message(
                "route test",
                chat_id_env=task_chat_env,
                idempotency_key="task-route-uuid",
            )
        self.assertEqual("om_task_chat", result["message_id"])
        auth_payload = request.call_args_list[0].args[1]
        message_payload = request.call_args_list[1].args[1]
        self.assertEqual("shared-app-id", auth_payload["app_id"])
        self.assertEqual("shared-app-secret", auth_payload["app_secret"])
        self.assertEqual("a-share-chat-id", message_payload["receive_id"])

    def test_remote_card_image_uses_existing_bot_credentials(self) -> None:
        env = {
            feishu_send.APP_ID_ENV_KEY: "app-id",
            feishu_send.APP_SECRET_ENV_KEY: "app-secret",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            feishu_send,
            "_download_remote_image",
            return_value=(b"png", "image/png", "card-image.png"),
        ), patch.object(
            feishu_send, "_get_tenant_access_token", return_value="tenant-token"
        ), patch.object(
            feishu_send, "_upload_image_bytes", return_value="img_card"
        ) as upload:
            image_key = feishu_send.upload_remote_image(
                "https://example.com/card-image.png"
            )
        self.assertEqual("img_card", image_key)
        self.assertEqual("tenant-token", upload.call_args.kwargs["bearer_token"])

    def test_codex_agent_retries_transient_failure(self) -> None:
        payload = structured_result(SUCCESS_NO_NOTIFY)
        attempts = []

        def fake_run(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
            attempts.append(command)
            if len(attempts) == 1:
                return CompletedProcess(command, 1, stdout="", stderr="temporary")
            result_path = Path(command[command.index("--output-last-message") + 1])
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            return CompletedProcess(command, 0, stdout="", stderr="")

        config = {
            "execution": {
                "timeout_seconds": 10,
                "retry_attempts": 2,
                "retry_backoff_seconds": 0,
            }
        }
        with patch.object(production_runner.shutil, "which", return_value="/usr/bin/codex"), patch.object(
            production_runner.subprocess, "run", side_effect=fake_run
        ):
            result = production_runner._codex_agent_runner("prompt", config, REPO_ROOT)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual(2, len(attempts))
        self.assertNotIn("--search", attempts[0])
        self.assertNotIn("--approve-for-me", attempts[0])
        self.assertIn("--output-schema", attempts[0])

    def test_agent_subprocess_environment_excludes_delivery_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FEISHU_APP_SECRET_SCHEDULE_TASK": "secret",
                "SOME_ACCESS_TOKEN": "token",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            environment = production_runner._agent_environment()
        self.assertEqual("/usr/bin", environment["PATH"])
        self.assertNotIn("FEISHU_APP_SECRET_SCHEDULE_TASK", environment)
        self.assertNotIn("SOME_ACCESS_TOKEN", environment)


if __name__ == "__main__":
    unittest.main()
