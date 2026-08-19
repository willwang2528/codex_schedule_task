# Feishu Card Delivery

Automation Hub uses Feishu Card 2.0 for production notifications. The Agent returns semantic card data; it never writes raw Feishu JSON. `scripts/feishu_cards.py` validates and escapes that data, then the Harness renders the final interactive payload.

## Task profiles

| Task | `delivery.presentation` | Delivery shape |
| --- | --- | --- |
| Agent Memory | `research_top5_cards` | Exactly five independent cards, one per ranked paper |
| A-share monitor | `market_dashboard_card` | Exactly three cards: blue overview, orange sentiment/mainline, yellow anomaly/risk |
| Apple price monitor | `price_alert_cards` | One card per qualifying SKU/offer, maximum five |

`SUCCESS_NO_NOTIFY` and `SKIPPED` always carry an empty card array and send nothing.

Task A separately configures `delivery.notification_triggers: ["11:20", "15:01"]`. All other market slots still collect and persist evidence, but the Harness deterministically clears attempted notification fields and never calls the Feishu adapter. Pending notification recovery enforces the same allowlist.

## Semantic card contract

Each card contains:

- template, title, subtitle, theme, and status tag
- one primary focus metric
- up to eight aligned label/value fields
- one to three grouped sections
- a verified primary-source link
- an optional public HTTPS image URL

The renderer applies Card 2.0 hierarchy, spacing, color, focus, grouping, truncation, dark/light-safe defaults, and a source button. Dynamic text is escaped so Agent output cannot inject mentions or card markup. Every rendered component is checked against a per-component field allowlist before delivery, preventing unsupported style properties from reaching the Feishu API.

For the three-card market profile, each card has exactly one visible conclusion followed by two sections inside a default-collapsed panel. The verified source button remains the final body element. Card 1 must expose the four indices, turnover, breadth, and limit activity in the first view; full OHLC, ladders, movers, timing, scope, conflicts, and missing-field explanations stay in details.

## Images

An optional image is accepted only from a public HTTPS host. The Harness blocks private/loopback targets, restricts image MIME types and size, uploads the resource through the existing Feishu bot credentials, and inserts the returned `image_key`.

Image download/upload failure is non-fatal: the same card is delivered without the image and the sanitized image error is retained in pending-delivery state. This also covers an app that has message-send permission but not the optional image-resource scope.

## Multi-card idempotency

Every card has its own deterministic Feishu UUID. State is persisted after each successful card. If card 3 of 5 fails, recovery sends only cards 3–5; cards 1–2 are not regenerated or resent. The business-event fingerprint still deduplicates the complete notification across task restarts.

## Validation

Project tests verify Card 2.0 structure and component field allowlists, exact Top 5 count, semantic escaping, bot adapter payload, optional-image fallback, unique per-card UUIDs, and partial-delivery recovery.

`lark-cli im +messages-send --as bot --msg-type interactive --dry-run` is used as a second request-shape check without sending a group message.
