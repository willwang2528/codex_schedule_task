# Codex Scheduled Task Setup

The official [Scheduled tasks documentation](https://learn.chatgpt.com/docs/automations) says local scheduled tasks can run in a selected project directory or an isolated Git worktree. The computer must remain powered on, the desktop app must remain running, and the local project must still be available when the run starts.

## Required preflight

Before enabling the daily task:

```bash
python3 scripts/validate_task.py smoke-test
python3 scripts/run_task.py smoke-test
python3 scripts/validate_task.py agent-memory-daily
python3 scripts/run_task.py agent-memory-daily
```

Then manually complete one full Agent Memory workflow in Codex and verify Candidate Discovery, Verification, Ranking, Top 5, local output, state, and optional Feishu delivery.

## Create Agent Memory Daily

In the desktop app, create a scheduled task with:

```text
Name: Agent Memory Daily
Schedule: Daily at 08:00
Timezone: Asia/Shanghai
Project: this repository
Execution location: local project
```

The local project is recommended for this task because deduplication state must persist between runs. Keep generated outputs and logs ignored, and review the first few runs. An isolated worktree is safer for source changes but requires a separate strategy for persistent state.

Use this short prompt:

```text
Open this repository.

Execute the task defined at:
tasks/agent-memory-daily/TASK.md

Follow:
AGENTS.md

and:
tasks/agent-memory-daily/task.yaml

Use available Codex web, research, browser, and local tools as needed.

Complete the entire workflow:
Discovery → Verification → Ranking → Top 5 → Save output → Feishu delivery.

First run:
python3 scripts/run_task.py agent-memory-daily

Do not use OpenAI API keys. Use the current Codex/ChatGPT runtime.
```

The scheduled prompt intentionally contains paths, not the long research prompt. Updating `TASK.md` therefore changes future behavior without recreating the scheduled task.

## Runtime environment

For Feishu delivery, make these values available to the desktop app process without committing them:

```text
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_CHAT_ID
```

When they are absent, research and local output can still succeed and delivery is `skipped`.

## Operations

- Keep the Mac awake and the desktop app running before 08:00.
- Keep the repository path stable.
- Review the first few scheduled runs before relying on unattended delivery.
- Inspect `logs/agent-memory-daily/<date>.log` and the local report after a failure.
- Re-run a failed delivery from the saved output; do not repeat research solely because Feishu failed.
