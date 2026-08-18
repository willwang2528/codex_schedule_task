# Automation Hub Architecture

## Design goal

Automation Hub keeps the scheduler generic and task behavior isolated.

```text
Scheduler / Codex scheduled task
              ↓
       task.yaml + TASK.md
              ↓
       shared skills + scripts
              ↓
     research or deterministic work
              ↓
        local output + state
              ↓
             delivery
              ↓
          Feishu chat
```

## Responsibilities

| Layer | Owns | Must not own |
| --- | --- | --- |
| Codex scheduled task | cadence, project, short pointer prompt | duplicated long business prompt |
| `tasks/<id>/` | objective, business rules, schedule metadata, paths | rules for unrelated tasks |
| `skills/` | reusable research, verification, ranking, delivery decisions | one task's full prompt |
| `scripts/` | validation, path preparation, HTTP delivery, deterministic checks | Agent Memory research policy |
| `outputs/` | finalized local artifacts | secrets |
| `state/` | deduplication and run continuity | access tokens |
| `logs/` | sanitized operational status | credentials or full API responses |

## Execution lifecycle

1. `scripts/run_task.py` resolves `tasks/<id>/task.yaml`.
2. `scripts/validate_task.py` validates schema, task isolation, and repository-relative paths.
3. The entrypoint prepares output, state, and daily log paths.
4. A configured deterministic script may run. Otherwise, the entrypoint returns `ready_for_codex` with the durable prompt path.
5. Codex follows `AGENTS.md`, the task prompt, and applicable shared skills.
6. The final result is saved locally before optional delivery.
7. State is updated only after finalization. Delivery status and sanitized errors are appended to the log.

`run_task.py` dispatches deterministic executors by a validated path from YAML; it does not switch on task ids. Research remains in the current Codex runtime, so no `OPENAI_API_KEY` is required.

## Task schema

```yaml
id: agent-memory-daily
name: Agent Memory Daily Brief
enabled: true

schedule:
  timezone: Asia/Shanghai
  cron: "0 8 * * *"

workflow:
  type: research_topk
  top_k: 5

task_prompt:
  path: tasks/agent-memory-daily/TASK.md

delivery:
  type: feishu
  enabled: true

output:
  save_local: true
  directory: outputs/agent-memory-daily

state:
  enabled: true
  path: state/agent-memory-daily.json
```

The repository uses a dependency-free mapping-only YAML parser. Task YAML supports nested mappings and scalar strings, booleans, numbers, and null values. Keep the long prompt in `TASK.md` and avoid YAML lists or multiline blocks.

## Logs and failure semantics

Each run appends a JSON line to:

```text
logs/<task-id>/<YYYY-MM-DD>.log
```

Every record contains `task`, `start_time`, `end_time`, `status`, `output_path`, `delivery_status`, and `error`.

- Research failure: log failure; do not create a fake result.
- Research success and delivery failure: retain output; mark delivery failed.
- Missing optional Feishu configuration: mark delivery skipped.
- Deterministic smoke failure: persist observations, return non-zero, and log the error.

Generated outputs and logs are ignored by Git. The initial Agent Memory state file is tracked so the schema and baseline are explicit; scheduled executions may update it locally.
