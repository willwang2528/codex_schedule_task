# Automation Hub

One runtime.
Many task definitions.
Shared workflow.
Shared delivery.

Automation Hub keeps durable task prompts in the repository and lets Codex scheduled tasks invoke them with a short pointer prompt.

```text
Codex Automation
       ↓
Task Definition
       ↓
Shared Skills
       ↓
Research / Execution
       ↓
Output
       ↓
Delivery
```

## Quick start

Validate and execute the deterministic smoke test:

```bash
python3 scripts/run_task.py smoke-test
```

Prepare the Agent Memory task for execution by the current Codex runtime:

```bash
python3 scripts/run_task.py agent-memory-daily
```

The second command validates configuration, creates output/state/log paths, and returns the prompt path. Codex then follows `AGENTS.md`, `TASK.md`, and the shared skills to perform research. It does not call the OpenAI API.

Configure Feishu only in the process environment:

```bash
cp config/env.example config/.env
```

Populate the ignored local `config/.env` file or export the values in the process environment. Never commit `config/.env`.

This repository uses namespaced Feishu variables to avoid collisions with other local projects:

```text
FEISHU_APP_ID_SCHEDULE_TASK
FEISHU_APP_SECRET_SCHEDULE_TASK
FEISHU_CHAT_ID_SCHEDULE_TASK
```

The Feishu module automatically loads these keys from `config/.env`; an explicitly exported process variable takes precedence over the file value.

## Add a task

```bash
cp -R tasks/_template tasks/<new-task>
```

Edit only `TASK.md` and `task.yaml`, then create a Codex scheduled task that points to the new task path. See [Add a task](docs/ADD_TASK.md) and [Codex Automation](docs/CODEX_AUTOMATION.md).

## Repository map

- `tasks/`: isolated business definitions
- `skills/`: reusable agent workflows
- `scripts/`: deterministic validation, execution plumbing, and delivery
- `outputs/`: generated reports, ignored except for `.gitkeep`
- `state/`: deduplication and run state
- `logs/`: sanitized runtime logs, ignored except for `.gitkeep`
- `docs/`: architecture and operations guidance

See [Architecture](docs/ARCHITECTURE.md) for lifecycle and failure semantics.
