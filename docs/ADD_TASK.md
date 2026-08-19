# Add a Task

## 1. Copy the template

```bash
cp -R tasks/_template tasks/<new-task>
```

## 2. Edit only the task definition

Update:

```text
tasks/<new-task>/TASK.md
tasks/<new-task>/task.yaml
```

At minimum, replace the id, name, prompt path, output directory, state path, schedule triggers, and business instructions. Trigger values use an inline JSON list such as `["09:00", "15:30"]`; timestamps use the configured IANA timezone.

Choose one delivery presentation:

- `post`: one Feishu rich-text post
- `research_top5_cards`: exactly five ranked research cards
- `market_dashboard_card`: exactly three progressive market cards
- `price_alert_cards`: one to five SKU/offer cards

Card profiles use the semantic fields in `config/task-result.schema.json`; Task prompts must not construct raw Feishu card JSON.

If every schedule slot must collect data but only selected slots may notify, add an inline `delivery.notification_triggers` list. The values must be a unique subset of `schedule.triggers`. The Harness suppresses Agent notification attempts outside the list and applies the same gate when recovering pending delivery.

Keep business policy in `TASK.md`. Reuse shared skills for research, verification, ranking, delivery, logging, and state. Add a deterministic script only when the work is a reusable mechanical operation; point to it with `workflow.deterministic_script` rather than adding a task-id branch to `run_task.py`.

When an Agent needs task-specific facts that should be collected deterministically before reasoning, place a JSON-emitting collector under that task directory and configure `workflow.context_script`. The Harness calls it with `--scheduled-at` and `--trigger-slot`, removes delivery secrets from its environment, and injects the returned object as deterministic workflow evidence. Optionally set `workflow.context_timeout_seconds`; collectors must preserve source scope, data time, freshness, and field-level failures.

## 3. Validate and test

```bash
python3 scripts/validate_task.py <new-task>
python3 scripts/run_task.py <new-task> --prepare-only
python3 scripts/validate_task.py --all
python3 scripts/scheduler.py --list
```

Run the full task manually before enabling its schedule:

```bash
python3 scripts/run_task.py <new-task> --execute-agent
```

Verify the structured JSON/Markdown output, atomic state update, JSONL log, conditional notification, and a `SUCCESS_NO_NOTIFY` result.

## 4. No Scheduler code change

The macOS LaunchAgent runs `scripts/scheduler.py`. It discovers every valid `tasks/*/task.yaml`; do not create a second per-task cron/Automation entry. See `docs/CODEX_AUTOMATION.md`.

## Isolation checklist

- No other task directory changed.
- No task-specific branch was added to shared Python.
- Primary-source and verification rules are explicit when research is involved.
- Output, state, and log paths are unique to the task.
- Secrets appear only in the runtime environment.
- The task does not depend on `OPENAI_API_KEY`.
- Agent output follows `config/task-result.schema.json`.
- Business event keys are stable enough for notification deduplication.
