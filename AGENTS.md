# Automation Hub Execution Contract

This file is the repository-wide execution contract. Task-specific instructions may add constraints but may not weaken these rules.

## Task-driven execution

Business behavior comes from both files below:

```text
tasks/<task-id>/TASK.md
tasks/<task-id>/task.yaml
```

Read and validate both before executing a task. Keep long prompts in `TASK.md`; keep schedules, paths, switches, and small scalar settings in `task.yaml`.

## Shared workflow

Reuse `skills/` for agent workflows and `scripts/` for deterministic mechanics. Do not place research, stock, pricing, or other task-specific business policy in the shared Python entrypoint.

## Task isolation

Adding or changing one task must not require edits to another task. A new task should normally require only a new `tasks/<task-id>/` directory; the generic Scheduler discovers valid definitions automatically.

## Research source order

For research tasks, evidence priority is:

```text
Primary or official source
> high-quality secondary source
> community discovery signal
```

Community sources may identify candidates but may not serve as final evidence.

## Verification before delivery

Research tasks must follow:

```text
Search
→ Candidate Pool
→ Verify
→ Rank
→ Final Output
→ Delivery
```

Never search for exactly the requested Top K and send those results without independent verification and ranking.

## Outputs, state, and failure handling

- Preserve a successful result locally even when delivery fails.
- On research failure, record the error and do not fabricate a result.
- The Agent returns only the structured result schema. It must not write state, outputs, logs, task files, or send Feishu itself.
- For card-enabled tasks, return only semantic `notification.cards` fields. Never construct raw Feishu Card JSON; the Harness owns validation, escaping, rendering, optional image upload, and multi-card delivery.
- Card renderer fields must match the per-component Card 2.0 documentation allowlist. Do not copy undocumented fields from style-guide examples; run the renderer tests and a real bot delivery check before enabling a new component shape.
- Read task state before selection and atomically write domain state only after a valid result is finalized.
- Failed results must use an empty state update and must not overwrite trusted business state.
- Use only `SUCCESS_NOTIFY`, `SUCCESS_NO_NOTIFY`, `SKIPPED`, or `FAILED`.
- Treat a materially changed publication, code release, or benchmark as an update, not a new item.
- Logs must include task id, run id, scheduled/start/finish timestamps, trigger slot, state version, status, notification state, output path, and a sanitized error.

## Secrets

- Use environment variables for credentials.
- Never commit secrets, tokens, cookies, private keys, or real `.env` files.
- Never place credentials in `TASK.md`, `README.md`, outputs, or logs.
- Feishu credentials for this repository must use the names `FEISHU_APP_ID_SCHEDULE_TASK`, `FEISHU_APP_SECRET_SCHEDULE_TASK`, and `FEISHU_CHAT_ID_SCHEDULE_TASK`. Do not fall back to unsuffixed names because other local projects may use them.
- Never print `FEISHU_APP_SECRET_SCHEDULE_TASK` or a complete access token.
- Do not add an `OPENAI_API_KEY` dependency. Use the current Codex/ChatGPT runtime and available tools.

## Delivery authorization

Delivery occurs only when the task enables it and its required environment variables are present. Missing optional delivery configuration is `skipped`, not a fatal task error. A configured delivery that fails is `failed`, while the local output remains available.
