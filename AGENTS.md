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

Adding or changing one task must not require edits to another task. A new task should normally require only a new `tasks/<task-id>/` directory and a new Codex scheduled task that points to it.

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
- Read task state before selection and write state only after a result is finalized.
- Treat a materially changed publication, code release, or benchmark as an update, not a new item.
- Logs must include task, start time, end time, status, output path, delivery status, and a sanitized error.

## Secrets

- Use environment variables for credentials.
- Never commit secrets, tokens, cookies, private keys, or real `.env` files.
- Never place credentials in `TASK.md`, `README.md`, outputs, or logs.
- Feishu credentials for this repository must use the names `FEISHU_APP_ID_SCHEDULE_TASK`, `FEISHU_APP_SECRET_SCHEDULE_TASK`, and `FEISHU_CHAT_ID_SCHEDULE_TASK`. Do not fall back to unsuffixed names because other local projects may use them.
- Never print `FEISHU_APP_SECRET_SCHEDULE_TASK` or a complete access token.
- Do not add an `OPENAI_API_KEY` dependency. Use the current Codex/ChatGPT runtime and available tools.

## Delivery authorization

Delivery occurs only when the task enables it and its required environment variables are present. Missing optional delivery configuration is `skipped`, not a fatal task error. A configured delivery that fails is `failed`, while the local output remains available.
