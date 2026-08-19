from __future__ import annotations

import hashlib
import json
import os
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
from feishu_send import FeishuDeliveryError
from production_runner import execute_production_task, recover_pending_delivery
from scheduler import discover_tasks, due_runs
from task_runtime import (
    FAILED,
    SKIPPED,
    SUCCESS_NO_NOTIFY,
    SUCCESS_NOTIFY,
    atomic_write_json,
    make_run_id,
    read_json_object,
    run_lock,
)
from validate_task import validate_all_tasks
from validate_task import load_task_config


def structured_result(
    status: str,
    *,
    updates: Dict[str, Any] | None = None,
    event_key: str = "",
    error: str = "",
    cards: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    should_notify = status == SUCCESS_NOTIFY
    return {
        "status": status,
        "should_notify": should_notify,
        "summary": "completed" if status in {SUCCESS_NOTIFY, SUCCESS_NO_NOTIFY} else "",
        "output_markdown": f"# {status}",
        "state_updates_json": json.dumps(updates or {}, ensure_ascii=False),
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


def semantic_card(
    template: str,
    *,
    rank: int = 1,
    image_url: str = "",
) -> Dict[str, Any]:
    return {
        "template": template,
        "title": f"Card {rank}",
        "subtitle": "2026-08-19 · verified",
        "theme": "blue" if template == "research_item" else "green",
        "tag": f"TOP {rank}" if template == "research_item" else "ALERT",
        "focus": {"value": f"#{rank}", "label": "Top research item"},
        "fields": [
            {"label": "Status", "value": "Published", "short": True},
            {"label": "Date", "value": "2026-08-19", "short": True},
        ],
        "sections": [
            {"title": "Summary", "content": "Verified contribution.", "collapsed": False},
            {"title": "Why it matters", "content": "Improves agent memory.", "collapsed": True},
            {"title": "Selection", "content": "Strong evidence.", "collapsed": True},
        ],
        "source": {"label": "Primary source", "url": "https://example.com/paper"},
        "image_url": image_url,
    }


def market_dashboard_cards() -> list[Dict[str, Any]]:
    cards = [semantic_card("market_dashboard", rank=index) for index in range(1, 4)]
    cards[0]["tag"] = "盘面总览"
    cards[0]["theme"] = "blue"
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
    cards[1]["tag"] = "情绪与主线"
    cards[1]["theme"] = "orange"
    cards[2]["tag"] = "异动与风险"
    cards[2]["theme"] = "yellow"
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

    def test_unchanged_task_c_prompt_is_byte_for_byte_migration(self) -> None:
        expected = {
            "agent-memory-frontier": "74230f5a1bcb92ab5b36148dd27ba677ffbdfc00531a0e147f928ee685ec8fbc",
        }
        for task_id, digest in expected.items():
            value = (REPO_ROOT / "tasks" / task_id / "TASK.md").read_bytes()
            self.assertEqual(digest, hashlib.sha256(value).hexdigest())

    def test_a_share_prompt_monitors_every_configured_trigger(self) -> None:
        config = load_task_config(REPO_ROOT / "tasks" / "a-share-monitor" / "task.yaml")
        prompt = (REPO_ROOT / "tasks" / "a-share-monitor" / "TASK.md").read_text(
            encoding="utf-8"
        )
        for trigger in config["schedule"]["triggers"]:
            self.assertIn(f"`{trigger}`", prompt)
        self.assertEqual(
            ["11:20", "15:01"], config["delivery"]["notification_triggers"]
        )
        self.assertIn("其余时点只采集和保存数据", prompt)
        self.assertIn("不得因为“尚未收盘”而跳过", prompt)
        self.assertIn("SUCCESS_NOTIFY", prompt)
        self.assertIn("Deterministic workflow evidence", prompt)

    def test_task_b_always_reports_daily_status(self) -> None:
        config = load_task_config(REPO_ROOT / "tasks" / "apple-price-monitor" / "task.yaml")
        prompt = (REPO_ROOT / "tasks" / "apple-price-monitor" / "TASK.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual("always", config["delivery"]["policy"])
        self.assertIn("每次成功执行都必须返回 `SUCCESS_NOTIFY`", prompt)
        self.assertIn("不得因“首次建立基线”", prompt)

    def test_scheduler_has_exact_daily_slots_and_ignores_disabled_smoke(self) -> None:
        tasks = {task["id"]: task for task in discover_tasks(REPO_ROOT)}
        self.assertEqual(
            ["09:20", "09:25", "09:35", "09:45", "11:20", "13:15", "14:30", "15:01"],
            tasks["a-share-monitor"]["triggers"],
        )
        self.assertEqual(["10:00"], tasks["apple-price-monitor"]["triggers"])
        self.assertEqual(["09:00"], tasks["agent-memory-frontier"]["triggers"])

        weekend = datetime(2026, 8, 22, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
        due = due_runs(REPO_ROOT, weekend)
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
        scheduled_at: datetime | None = None,
        trigger_slot: str = "09:20",
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if sender is not None:
            kwargs["delivery_sender"] = sender
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
        self.config["delivery"]["notification_triggers"] = ["11:20", "15:01"]
        calls = []
        result = self.execute(
            structured_result(
                SUCCESS_NOTIFY,
                event_key="must-not-send",
                updates={"snapshot": {"breadth": "verified"}},
            ),
            sender=lambda *args, **kwargs: calls.append(kwargs),
            trigger_slot="09:35",
        )
        state = read_json_object(self.state_path)
        self.assertEqual(SUCCESS_NO_NOTIFY, result["status"])
        self.assertEqual("not_allowed_for_trigger", result["delivery_status"])
        self.assertEqual([], calls)
        self.assertEqual({"breadth": "verified"}, state["snapshot"])
        self.assertEqual({}, state["_runtime"]["notifications"])

    def test_notification_trigger_requires_success_notify(self) -> None:
        self.config["delivery"]["notification_triggers"] = ["11:20", "15:01"]
        result = self.execute(
            structured_result(SUCCESS_NO_NOTIFY),
            trigger_slot="11:20",
        )
        self.assertEqual(FAILED, result["status"])
        self.assertIn("requires SUCCESS_NOTIFY", result["error"])

    def test_recovery_suppresses_pending_notification_from_data_only_slot(self) -> None:
        self.config["delivery"]["notification_triggers"] = ["11:20", "15:01"]
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
    def test_card_2_structure_has_one_focus_and_grouped_details(self) -> None:
        card = feishu_cards.render_card(semantic_card("research_item", rank=1))
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("default", card["config"]["width_mode"])
        tags = [element["tag"] for element in card["body"]["elements"]]
        self.assertEqual(
            ["column_set", "markdown", "collapsible_panel", "button"], tags
        )
        focus_markdown = card["body"]["elements"][0]["columns"][0]["elements"][0]
        self.assertTrue(focus_markdown["content"].startswith("## "))
        focus_column = card["body"]["elements"][0]["columns"][0]
        self.assertNotIn("corner_radius", focus_column)
        button = card["body"]["elements"][-1]
        self.assertEqual("open_url", button["behaviors"][0]["type"])
        panel = card["body"]["elements"][2]
        self.assertFalse(panel["expanded"])
        self.assertEqual(
            "详细数据（点击展开/收起）", panel["header"]["title"]["content"]
        )

    def test_market_card_requires_hidden_detail_section(self) -> None:
        cards = market_dashboard_cards()
        cards[0]["sections"] = [
            {"title": "Summary", "content": "Only a summary.", "collapsed": False}
        ]
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "requires a collapsed detail section"
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

        cards[2]["theme"] = "red"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "risk card theme must be yellow"
        ):
            feishu_cards.validate_presentation(
                "market_dashboard_card", cards, should_notify=True
            )

    def test_card_renderer_rejects_undocumented_component_fields(self) -> None:
        card = feishu_cards.render_card(semantic_card("research_item", rank=1))
        card["body"]["elements"][0]["columns"][0]["corner_radius"] = "8px"
        with self.assertRaisesRegex(
            feishu_cards.CardSpecError, "unsupported column fields: corner_radius"
        ):
            feishu_cards.validate_rendered_card(card)

    def test_card_renderer_escapes_agent_markup(self) -> None:
        spec = semantic_card("research_item", rank=1)
        spec["sections"][0]["content"] = "<at id=all></at> **unsafe**"
        card = feishu_cards.render_card(spec)
        content = card["body"]["elements"][1]["content"]
        self.assertNotIn("<at", content)
        self.assertIn("&lt;at", content)

    def test_private_image_url_is_rejected(self) -> None:
        with self.assertRaises(FeishuDeliveryError):
            feishu_send._assert_public_https_url("https://127.0.0.1/image.png")


class AdapterAndRetryTests(unittest.TestCase):
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
