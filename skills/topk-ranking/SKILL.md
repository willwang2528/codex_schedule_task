---
name: topk-ranking
description: Rank independently verified research candidates by scientific value and select a defensible Top K.
---

# Top-K Ranking

Rank only verified candidates. Use task-specific criteria when provided; otherwise score:

| Criterion | Weight |
| --- | ---: |
| Research significance | 25 |
| Novelty | 20 |
| Topic relevance | 20 |
| Evidence quality | 15 |
| Source credibility | 10 |
| Recency | 10 |

Scientific value dominates recency. Prefer the newer item only when overall quality is close.

Before finalizing each item, answer: "Why does this deserve one of today's limited positions?" Remove any item without a concrete answer. Apply state-based deduplication; allow a previously seen work only for a material update and label it `Update`.

Keep internal scores and rejection reasons available for audit, while matching the task's requested final format.
