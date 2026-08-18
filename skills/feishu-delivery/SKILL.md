---
name: feishu-delivery
description: Deliver a finalized Automation Hub result to Feishu without exposing credentials or losing local output on delivery failure.
---

# Feishu Delivery

Use this workflow only after verification, ranking, and local output persistence.

## Preconditions

- The task enables Feishu delivery.
- `FEISHU_APP_ID_SCHEDULE_TASK`, `FEISHU_APP_SECRET_SCHEDULE_TASK`, and `FEISHU_CHAT_ID_SCHEDULE_TASK` are available in the process environment or the ignored local file `config/.env`.
- The exact final output has already been saved locally.

Explicit process environment values take precedence over `config/.env`. Do not read unsuffixed Feishu variables. If all namespaced variables are absent, mark delivery `skipped`. If only some are present, mark it `failed` and report incomplete configuration without printing values.

## Delivery

Use `python3 scripts/feishu_send.py --type post --file <output-path> --title <title>` for a rich-text post, or `--type text` for a plain smoke message. The script obtains a tenant access token and sends with `receive_id_type=chat_id`.

Never log an app secret, access token, cookie, or credential-bearing response. On API failure, retain the local result, mark `delivery_status=failed`, record only the sanitized code or message, and stop rather than retrying indefinitely.
