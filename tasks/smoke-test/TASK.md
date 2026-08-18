# Smoke Test

## Objective

Verify that:

1. Codex can trigger the task.
2. The task can read this repository.
3. The runtime can access a public website.
4. The runtime can execute a local script.
5. The task can produce a local output.
6. Feishu delivery works when configuration is present.

## Workflow

1. Access at least one stable public endpoint from `github.com`, `arxiv.org`, or `openreview.net`.
2. Get the current timestamp in the configured timezone.
3. Confirm the repository and local script are available.
4. If all Feishu environment variables are present, send the smoke-test message. If none are present, mark Feishu as `skipped`. Treat partial configuration as `failed` without discarding the output.
5. Save the final report to `outputs/smoke-test/<timestamp>.md`.

## Output Format

```text
Automation Hub OK

timestamp: <ISO 8601>
repository: codex_schedule_task
network: ok | failed
local_script: ok | failed
feishu: ok | skipped | failed
```

## Failure Handling

- Always persist the observed status when the output directory is writable.
- A missing optional Feishu configuration is not fatal.
- Do not print credentials or access tokens.
