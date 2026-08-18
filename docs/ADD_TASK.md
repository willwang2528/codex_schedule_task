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

At minimum, replace the id, name, prompt path, output directory, state path, schedule, and business instructions.

Keep business policy in `TASK.md`. Reuse shared skills for research, verification, ranking, delivery, logging, and state. Add a deterministic script only when the work is a reusable mechanical operation; point to it with `workflow.deterministic_script` rather than adding a task-id branch to `run_task.py`.

## 3. Validate and test

```bash
python3 scripts/validate_task.py <new-task>
python3 scripts/run_task.py <new-task> --prepare-only
```

Run the full task manually in Codex before enabling its schedule. Verify the local output, state update, log record, and delivery behavior.

## 4. Create the Codex scheduled task

Create a new scheduled task that points to:

```text
tasks/<new-task>/TASK.md
```

Use a short pointer prompt; do not copy the durable business prompt into the scheduler. See `docs/CODEX_AUTOMATION.md`.

## Isolation checklist

- No other task directory changed.
- No task-specific branch was added to shared Python.
- Primary-source and verification rules are explicit when research is involved.
- Output, state, and log paths are unique to the task.
- Secrets appear only in the runtime environment.
- The task does not depend on `OPENAI_API_KEY`.
