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

Do not commit `config/.env`. Export those values into the environment used by the Codex scheduled task before delivery.

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
