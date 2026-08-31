# macOS Automation Hub Scheduler

本项目使用一个 macOS LaunchAgent 常驻运行通用 Scheduler。不要再为三个 Task 分别创建 Codex Automation 或 cron，否则会产生重复执行入口。

## 安装前检查

```bash
cd ~/project/schedule_task/codex_schedule_task
python3 scripts/validate_task.py --all
python3 scripts/scheduler.py --list
python3 -m unittest discover -s tests -v
python3 scripts/scheduler.py --run-due \
  --at '2026-08-19T10:00:00+08:00' \
  --dry-run
```

## 安装和管理

```bash
python3 scripts/manage_scheduler.py install
python3 scripts/manage_scheduler.py status
python3 scripts/manage_scheduler.py stop
python3 scripts/manage_scheduler.py start
```

安装位置：

```text
~/Library/LaunchAgents/com.will.automation-hub.scheduler.plist
```

它通过系统自带的 `caffeinate -i` 常驻运行，每 30 秒检查一次时间点：显示器仍可休眠，但系统不会因闲置而睡眠。它仅运行 `enabled: true` 的 Task；已完成 run_id 会从持久状态中识别并跳过；投递失败的 pending notification 会优先恢复，不会重跑 Agent。

## Runtime environment

For Feishu delivery, make these values available to the desktop app process without committing them:

```text
FEISHU_APP_ID_SCHEDULE_TASK
FEISHU_APP_SECRET_SCHEDULE_TASK
FEISHU_CHAT_ID_SCHEDULE_TASK
```

The Feishu module automatically loads these namespaced keys from the ignored local file `config/.env`. Explicit process environment values take precedence. Unsuffixed Feishu variable names are intentionally unsupported to avoid collisions with other projects.

All tasks share the same App ID and App Secret. A task may override only its destination by setting `delivery.chat_id_env` to a namespaced local variable such as `FEISHU_CHAT_ID_A_SHARE_MONITOR_SCHEDULE_TASK`; tasks without this field continue using `FEISHU_CHAT_ID_SCHEDULE_TASK`.

Task A also uses `delivery.notification_triggers: ["09:35", "11:20", "15:01"]`. Its other five daily slots remain active data runs, but cannot create, send, or recover a Feishu notification.

Task A separately enables `delivery.failure_alert` for all eight sampling slots. A failed data-only slot, failed notification slot, Agent/validation failure, or Feishu delivery failure creates one sanitized Post alert in the Task A chat. This alert is independent from the normal notification allowlist and uses a stable idempotency key. If the alert cannot be sent, it remains pending and the Scheduler retries it before later due runs.

The same failure alert also creates one idempotent request under `logs/self-repair/a-share-monitor/` and starts a bounded background Codex repair Agent. The Agent receives the exact sanitized alert and local output paths, works with `workspace-write`, follows RED-GREEN-refactor, and runs the repository quality gate. It is isolated from Feishu credentials and may not send messages, rerun scheduled business tasks, commit, push, or recursively launch another repair Agent. Launcher or repair failure is recorded separately and never blocks the original alert.

Task C uses the same failure-alert and self-repair contract for its `09:00` run, with requests stored under `logs/self-repair/agent-memory-frontier/`. Disabled Task B remains excluded from scheduling, Feishu delivery, alerts, and repair Agents.

Task A requires Feishu readback for its three normal notification slots and for failure alerts. A message is marked `sent` only after the returned `message_id` can be read from the Task A chat; if POST was accepted but GET verification fails, recovery reuses that ID and does not send a duplicate. The Scheduler also checks `11:20` and `15:01` ten minutes after each slot. A missing/failed run, or a successful notification without `sent`/`pending` delivery state, creates an alert and writes `logs/scheduler/health.jsonl`.

Agent output is constrained twice: `config/task-result.schema.json` is supplied to Codex and revalidated locally, then Harness semantic rules run. Domain state uses a strict operation list with an enumerated namespace and per-task `state.allowed_update_keys`. One correction retry receives exact JSON-pointer errors; no state, pending record, or delivery is created before both layers pass.

Its optional daily archive Pins the first successfully delivered `15:01` card only after all three closing cards are sent. This is deliberately best-effort: a Pin permission or network error is recorded separately and does not make the original notification fail.

缺少配置时，真正需要通知的执行会保留 pending notification 并返回明确失败；补齐配置后可用以下命令恢复，不重复研究：

```bash
python3 scripts/run_task.py <task-id> --recover-pending
```

## Operations

- Keep the Mac powered on and logged in; LaunchAgent belongs to the current GUI user session. Manual sleep or shutdown still suspends execution.
- Keep the repository path stable.
- Review the first few scheduled runs before relying on unattended delivery.
- Inspect `logs/scheduler/health.jsonl`, `logs/<task-id>/<date>.log`, and the local run output after a failure.
- Re-run a failed delivery from the saved output; do not repeat research solely because Feishu failed.
- Treat a failure alert as an incident marker: inspect its run ID and output paths, then fix the task. Recovery of the alert does not change the original run from `FAILED` to success.
