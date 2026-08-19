# Automation Hub

一个通用 Scheduler，加载多个相互隔离的 Agent Task，并统一处理结构化结果、事务状态、条件通知、日志和恢复。

```text
task.yaml + TASK.md
        ↓
generic scheduler
        ↓
structured Codex run
        ↓
atomic state + local output
        ↓
conditional Feishu delivery
```

## 快速检查

```bash
cd ~/project/schedule_task/codex_schedule_task
python3 scripts/validate_task.py --all
python3 scripts/scheduler.py --list
python3 -m unittest discover -s tests -v
```

原有 deterministic smoke-test 保留，可手工执行：

```bash
python3 scripts/run_task.py smoke-test
```

## 手工执行正式任务

```bash
python3 scripts/run_task.py a-share-monitor --execute-agent
python3 scripts/run_task.py apple-price-monitor --execute-agent
python3 scripts/run_task.py agent-memory-frontier --execute-agent
```

只检查指定时间应触发哪些任务，不执行 Agent 或发送飞书：

```bash
python3 scripts/scheduler.py --run-due \
  --at '2026-08-19T10:00:00+08:00' \
  --dry-run
```

## macOS 自动调度

```bash
python3 scripts/manage_scheduler.py install
python3 scripts/manage_scheduler.py status
```

LaunchAgent 通过系统自带的 `caffeinate -i` 常驻运行通用 Scheduler：显示器仍可休眠，系统不会因闲置而睡眠。手工睡眠或关机期间不会执行；恢复后会在各任务的 catch-up 窗口内补跑尚未完成的时间点。

## 飞书本地配置

```bash
cp config/env.example config/.env
chmod 600 config/.env
```

只在被 Git 忽略的 `config/.env` 中设置：

```text
FEISHU_APP_ID_SCHEDULE_TASK
FEISHU_APP_SECRET_SCHEDULE_TASK
FEISHU_CHAT_ID_SCHEDULE_TASK
```

`scripts/feishu_send.py` 会自动加载该文件；进程环境中的同名变量优先。凭据不会写入 Task、state、output 或 log。

任务默认使用 `FEISHU_CHAT_ID_SCHEDULE_TASK`。需要单独群聊时，在该任务的 `delivery.chat_id_env` 指定形如 `FEISHU_CHAT_ID_<TASK>_SCHEDULE_TASK` 的本地变量；App ID 与 App Secret 仍复用上述两个全局变量。

需要“所有时点采集、仅部分时点通知”时，在 `delivery.notification_triggers` 设置通知白名单。Task A 固定为 `["11:20", "15:01"]`；其他时点仍生成输出和状态，但 Harness 不创建或恢复飞书通知。

正式通知使用 Feishu Card 2.0：Agent Memory 固定五张研究卡，A 股正常交易日使用“盘面总览—情绪与主线—异动与风险”三张渐进式市场卡，Apple 使用逐 SKU 价格卡。A 股竞品调研与取舍见 [A 股分析产品复盘调研](docs/A_SHARE_COMPETITIVE_REVIEW.md)，实现与降级策略见 [飞书卡片投递](docs/FEISHU_CARDS.md)。

## 新增任务

```bash
cp -R tasks/_template tasks/<new-task>
```

通常只需编辑 `TASK.md` 和 `task.yaml`；Scheduler 会自动发现，无需修改核心 Python。详见 [新增任务](docs/ADD_TASK.md)、[架构](docs/ARCHITECTURE.md) 和 [macOS 调度](docs/CODEX_AUTOMATION.md)。

## 目录

- `tasks/`：Prompt 与任务配置
- `scripts/`：Scheduler、Runner、状态事务和飞书适配器
- `state/`：跨进程业务状态与幂等记录
- `outputs/`：每次运行的 JSON/Markdown，Git 忽略
- `logs/`：逐次结构化日志，Git 忽略
- `config/`：结构化结果 schema 与本地环境示例
- `tests/`：无真实飞书副作用的运行时测试
