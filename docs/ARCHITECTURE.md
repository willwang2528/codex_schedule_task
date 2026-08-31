# Automation Hub Architecture

## Design goal

Automation Hub keeps the scheduler generic and task behavior isolated.

```text
launchd → generic scheduler → task.yaml + TASK.md
                              ↓
                       structured Codex Agent
                              ↓
                strict Schema → correction retry
                              ↓
                 Harness semantic validation
                              ↓
              atomic state + pending notification
                              ↓
              Feishu delivery → message readback
                              ↓
                slot monitor + independent log
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
5. The Runner validates the result against the same checked-in Schema locally, then the Harness validates status, notification fields, presentation, source metadata, task-owned state namespaces, and cross-field business rules. A rejected result receives exact JSON-pointer errors and one correction attempt.
6. If the second result differs only by forbidden Harness-owned state operations, the Harness removes those operations and records a warning. For an invalid `value_json`, it may restore only unambiguous missing closing delimiters or preserve the longest complete prefix before a malformed tail; original and recovered strings remain in local validation artifacts. It never repairs cards, market facts, unknown namespaces, unterminated strings, or ambiguous scalar values.
7. JSON/Markdown outputs are saved under `outputs/<task-id>/<YYYY-MM-DD>/`.
8. Only a valid non-failed result can merge domain state. The update target is a strict operation list (`namespace`, `operation=upsert`, `value_json`); each task has an explicit namespace allowlist. State is written with temp file + fsync + atomic rename.
9. `SUCCESS_NOTIFY` creates a stable pending notification before delivery. Card-profile tasks render semantic data into Feishu Card 2.0; every card receives its own deterministic UUID and is checkpointed after sending. Partial multi-card failure remains recoverable without rerunning the Agent.
10. When readback is configured, a Feishu POST response is only delivery acceptance: the message remains pending until GET readback confirms the same `message_id` in the configured task chat. Recovery retries readback without resending an already accepted message.
11. A failed slot with `delivery.failure_alert` enabled creates a separate deterministic Post alert before its delivery attempt. Alert delivery bypasses the business-notification time allowlist, has its own idempotency key, is never added to the business notification history or daily Pin archive, and remains pending if the alert channel is temporarily unavailable.
12. When `failure_alert.self_repair` is enabled, the same incident independently creates one local repair request and starts one background `workspace-write` Codex Agent. The Agent reads the alert and local artifacts, follows TDD, and records status/response under `logs/self-repair/<task-id>/`; it cannot block alert delivery and cannot commit, push, send messages, rerun business tasks, or recursively launch repairs.
13. After each configured notification grace period, the Scheduler requires a terminal `sent`, `pending`, or justified `SKIPPED` result. Missing, failed, or inconsistent runs produce an alert, use the same optional repair path, and write an independent record in `logs/scheduler/health.jsonl`.
14. A timezone-aware JSONL run record is appended to `logs/<task-id>/`.

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
  # Optional best-effort group Pin archive for selected notification slots:
  # daily_archive: {"enabled": true, "trigger_slots": ["15:01"]}
  # Optional deterministic failure alerts; values may include data-only slots:
  # failure_alert: {"enabled": true, "trigger_slots": ["09:00", "15:00"], "self_repair": {"enabled": true, "timeout_seconds": 7200}}
  # Optional POST/GET verification in the configured task chat:
  # readback: {"enabled": true, "trigger_slots": ["09:00"], "include_failure_alerts": true, "retry_attempts": 2}
  # Optional post-grace terminal-state monitor; slots must also be notification-enabled and failure-alerted:
  # completion_monitor: {"enabled": true, "trigger_slots": ["09:00"], "grace_minutes": 10}
  policy: conditional
  presentation: post
  retry_attempts: 2

output:
  save_local: true
  directory: outputs/example-task

state:
  enabled: true
  path: state/example-task.json
  allowed_update_keys: ["example_domain_state"]

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
- A message requiring readback is `sent` only after the exact message is found in the exact configured chat. An accepted but unreadable message keeps its `message_id` and pending state, so recovery does not duplicate it.
- Configured failed slots create one sanitized failure alert. A failed alert remains independently pending and is retried without rerunning the Agent.
- Notification-slot completion failures also append `logs/scheduler/health.jsonl`, so an outage in the Feishu path does not erase the local incident record.
- No meaningful event is `SUCCESS_NO_NOTIFY`, not an error.
- Non-trading day or other valid no-op is `SKIPPED`, not an error.
- Deterministic smoke failure: persist observations, return non-zero, and log the error.

Generated outputs, logs, and runtime `state/*.json` files are ignored by Git. State structure is defined by the result schema and tests; unattended executions update only local state.
