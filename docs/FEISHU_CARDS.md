# Feishu Card Delivery

Automation Hub uses Feishu Card 2.0 for production notifications. The Agent returns semantic card data; it never writes raw Feishu JSON. `scripts/feishu_cards.py` validates and escapes that data, then the Harness renders the final interactive payload.

## Task profiles

| Task | `delivery.presentation` | Delivery shape |
| --- | --- | --- |
| Agent Memory | `research_top5_cards` | Exactly five independent cards, one per ranked paper |
| A-share monitor | `market_dashboard_card` | Exactly three cards: direction-colored overview, profit/loss-colored sentiment/mainline, yellow anomaly/risk |
| Apple price monitor (disabled) | `price_alert_cards` | Task and delivery are disabled; retained only for possible future reactivation |
| GitHub Repo of the Day | `repo_digest_card` | Exactly one project card: visible one-sentence positioning, collapsed workflow and conclusion, verified repository button |

`SUCCESS_NO_NOTIFY` and `SKIPPED` always carry an empty card array and send nothing.

Task A separately configures `delivery.notification_triggers: ["09:35", "11:20", "15:01"]`. All other market slots still collect and persist evidence, but the Harness deterministically clears attempted notification fields and never calls the Feishu adapter. Pending notification recovery enforces the same allowlist.

Task A also enables `delivery.daily_archive` only for `15:01`. After all three original cards are delivered, the Harness Pins the first `盘面总览` card. The group-wide Pin list therefore contains one closing-review entry per trading day; selecting it returns the reader to the three adjacent closing cards. Pin failure is stored under the notification's `daily_archive` state and never downgrades successful card delivery.

Failure alerts intentionally use a single deterministic rich-text Post instead of an Agent-generated card. This keeps the alert path independent from card-schema failures. The Post includes task, scheduled time, trigger slot, run ID, sanitized stage/error, and local output paths. It has its own pending state and idempotency key, bypasses Task A's normal notification time allowlist, and is excluded from the business notification history and daily Pin archive.

For Task A, HTTP send acceptance is not the final delivery state. The Harness saves the returned `message_id`, then reads that exact message back and verifies its `chat_id` against `FEISHU_CHAT_ID_A_SHARE_MONITOR_SCHEDULE_TASK`. Until that succeeds the message stays pending. Recovery retries only the readback when the accepted message ID is already present, avoiding duplicate cards.

## Semantic card contract

Each card contains:

- template, title, subtitle, theme, and status tag
- one primary focus metric
- up to eight aligned label/value fields
- one to six grouped sections, with profile-specific limits
- a verified primary-source link
- an optional public HTTPS image URL

The renderer applies Card 2.0 hierarchy, spacing, color, focus, grouping, truncation, dark/light-safe defaults, and a source button. Dynamic text is escaped so Agent output cannot inject mentions or card markup. Every rendered component is checked against a per-component field allowlist before delivery, preventing unsupported style properties from reaching the Feishu API.

Standalone Codex automations can pass one semantic JSON object through `scripts/send_semantic_card.py`. The script validates the selected presentation, renders Card 2.0, sends with a stable idempotency key, persists the accepted `message_id` atomically, and reads it back from the configured chat. If readback fails, rerunning with the same card, key, and receipt retries only readback instead of sending another card.

For the Agent Memory profile, the card header shows the exact original paper title and its Chinese translation. The first visible body section is `一句话概述`: one sentence based on the full text that states only the problem and method, without experimental data or result claims. The default-collapsed `论文详解` panel then starts with `论文摘要翻译`, followed by the existing sections `现存问题` → `已有方法的不足` → `当前方法为什么可行` → `未来展望`. Agent Memory relevance and selection rationale remain concise visible fields. This is a presentation contract only; candidate search, source verification, filtering, and Top 5 ranking are unchanged.

For the three-card market profile, the visible conclusion is the first body element, followed by the focus/field block, two detail sections inside a default-collapsed `证据详情` panel, and the final verified-source button. The fixed section sets are `盘面结论 / 指数与阶段变化 / 数据口径与核验`, `情绪与主线结论 / 梯队、代表股与驱动 / 持续性与证据边界`, and `风险与验证结论 / 异动与风险证据 / 待验证清单与数据口径`. Card 1 exposes the four indices, turnover, breadth, and limit activity; full OHLC, ladders, movers, timing, scope, conflicts, and missing-field explanations stay in details.

Market colors follow the user-facing Chinese-market convention while preserving text labels: clear broad rise / broad money-making uses red, clear broad fall / broad loss effect uses green, mixed or flat overview uses blue, rotation/divergence uses orange, insufficient evidence uses grey, and the risk card remains yellow. Every change still includes a sign, number, and direction word so meaning never depends on color alone.

## Images

An optional image is accepted only from a public HTTPS host. The Harness blocks private/loopback targets, restricts image MIME types and size, uploads the resource through the existing Feishu bot credentials, and inserts the returned `image_key`.

Image download/upload failure is non-fatal: the same card is delivered without the image and the sanitized image error is retained in pending-delivery state. This also covers an app that has message-send permission but not the optional image-resource scope.

## Multi-card idempotency

Every card has its own deterministic Feishu UUID. State is persisted after each successful card. If card 3 of 5 fails, recovery sends only cards 3–5; cards 1–2 are not regenerated or resent. The business-event fingerprint still deduplicates the complete notification across task restarts.

## Validation

Project tests verify Card 2.0 structure and component field allowlists, exact Top 5 count, semantic escaping, bot adapter payload, optional-image fallback, unique per-card UUIDs, and partial-delivery recovery.

`lark-cli im +messages-send --as bot --msg-type interactive --dry-run` is used as a second request-shape check without sending a group message.
