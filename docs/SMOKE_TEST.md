# Automation Hub Smoke Test

The original deterministic `smoke-test` remains available and is intentionally `enabled: false`, so the production Scheduler never runs it automatically.

Manual execution:

```bash
python3 scripts/run_task.py smoke-test
```

It verifies repository visibility, local script execution, outbound network connectivity, local output creation, and—when configured—the existing Feishu adapter. It does not validate production business data. Production runtime behavior is covered with mocked delivery in:

```bash
python3 -m unittest discover -s tests -v
```
