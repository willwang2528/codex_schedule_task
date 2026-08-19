# Automation Hub Architecture

## Design goal

Automation Hub keeps the scheduler generic and task behavior isolated.

```text
launchd → generic scheduler → task.yaml + TASK.md
                              ↓
                       structured Codex Agent
                              ↓
                 validate result + save outputs
                              ↓
                    atomic state transaction
                              ↓
                 conditional idempotent delivery
```

## Responsibilities

| Layer | Owns | Must not own |
| --- | --- | --- |
| launchd + Scheduler | discover enabled tasks and trigger due slots | task-specific business logic |
| `tasks/<id>/` | objective, business rules, schedule metadata, paths | rules for unrelated tasks or secrets |
| `skills/` | reusable research, verification, ranking, delivery decisions | one task's full prompt |
| Runner/Harness | prompt/state loading, result validation, transaction, dedup, delivery, logs | stock, pricing, or paper-selection policy |
| Codex Agent | data gathering and business judgment; structured result proposal | direct state writes or message delivery |
| `outputs/` | finalized local artifacts | secrets |
| `state/` | deduplication and run continuity | access tokens |
| `logs/` | sanitized operational status | credentials or full API responses |

## Execution lifecycle

1. `scripts/run_task.py` resolves `tasks/<id>/task.yaml`.
2. `scripts/validate_task.py` validates schema, task isolation, and repository-relative paths.
3. The Runner creates a deterministic run_id, serializes the task, and persists a recoverable `RUNNING` record.
4. The Codex Agent receives the immutable business Prompt plus bounded relevant state and returns the strict schema in `config/task-result.schema.json`.
5. The Harness validates status, notification fields, source freshness metadata, and proposed domain-state updates.
6. JSON/Markdown outputs are saved under `outputs/<task-id>/<YYYY-MM-DD>/`.
7. Valid non-failed domain state is merged and written with temp file + fsync + atomic rename. A failed result changes only operational retry metadata.
8. `SUCCESS_NOTIFY` creates a stable pending notification before delivery. Card-profile tasks render semantic data into Feishu Card 2.0; every card receives its own deterministic UUID and is checkpointed after sending. Partial multi-card failure remains recoverable without rerunning the Agent.
9. A timezone-aware JSONL run record is appended to `logs/<task-id>/`.

`run_task.py` and `scheduler.py` do not switch on task IDs. The existing deterministic smoke executor remains supported.

## Task schema

```yaml
id: example-task
name: Example Task
enabled: true

schedule:
  timezone: Asia/Shanghai
  triggers: ["09:00", "15:00"]
  catch_up_minutes: 60

workflow:
  type: codex_structured
  # Optional task-owned JSON evidence collector executed before the Agent:
  # context_script: tasks/example-task/collect_context.py
  # context_timeout_seconds: 90

task_prompt:
  path: tasks/example-task/TASK.md

execution:
  timeout_seconds: 1200
  retry_attempts: 2
  retry_backoff_seconds: 20

delivery:
  type: feishu
  enabled: true
  target: configured_chat
  # Optional per-task destination; shared App ID/Secret remain unchanged:
  # chat_id_env: FEISHU_CHAT_ID_EXAMPLE_TASK_SCHEDULE_TASK
  # Optional hard allowlist: all schedule slots still run, only these may notify:
  # notification_triggers: ["09:00"]
  policy: conditional
  presentation: post
  retry_attempts: 2

output:
  save_local: true
  directory: outputs/example-task

state:
  enabled: true
  path: state/example-task.json

logging:
  directory: logs/example-task
```

The repository uses a dependency-free YAML subset parser. It supports nested mappings, scalar values, and inline JSON arrays/objects. Keep the long prompt in `TASK.md`; do not use multiline YAML blocks.

## Logs and failure semantics

Each run appends a JSON line to:

```text
logs/<task-id>/<YYYY-MM-DD>.log
```

Every production record contains `task_id`, `run_id`, `scheduled_at`, `started_at`, `finished_at`, `status`, `trigger_slot`, `state_version`, `notification_sent`, `delivery_status`, `output_path`, and `error`.

- Agent/business failure: retain diagnostic output; do not merge proposed domain state.
- Agent success and delivery failure: retain output and committed domain state; keep notification pending for recovery.
- No meaningful event is `SUCCESS_NO_NOTIFY`, not an error.
- Non-trading day or other valid no-op is `SKIPPED`, not an error.
- Deterministic smoke failure: persist observations, return non-zero, and log the error.

Generated outputs, logs, and runtime `state/*.json` files are ignored by Git. State structure is defined by the result schema and tests; unattended executions update only local state.
