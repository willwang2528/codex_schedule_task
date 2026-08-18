# Agent Memory Daily Research Brief

## Objective

Every run produces an evidence-grounded **Agent Memory Daily Research Brief** for computer-science researchers working on agents, GUI agents, and agent memory.

The final brief contains only **Top 5**, but the workflow must first build and verify a larger candidate pool. Never search for five items and send the first five results.

## Scope

The core topic is:

```text
Agent Memory
```

Search both the core and its research neighborhood:

```text
Long-horizon Agent
Continual / Lifelong Agent
Context Management
Experience / Trajectory Reuse
Retrieval / RAG for Agents
Skill / Procedural Memory
Reflection / Self-improvement
World Model
Belief State
Personalization
GUI Agent Memory
Web Agent Memory
Persistent Agent State
Memory Policy
Memory Management
Memory Consolidation
Memory Forgetting
Memory Retrieval
Memory Recollection
```

Aim loosely for about 70% directly about Agent Memory and 30% neighboring work that could change the Agent Memory research paradigm. This is not a quota. Quality and mechanism-level relevance take priority.

## Sources

Use primary sources for final evidence. Apply this priority order.

### Priority 1 — Published research

Prefer strong peer-reviewed work from:

```text
NeurIPS
ICML
ICLR
ACL
EMNLP
NAACL
AAAI
IJCAI
CHI
UIST
```

Verify publication through sources such as ACL Anthology, PMLR, ACM Digital Library, IEEE, or official conference proceedings.

### Priority 2 — High-quality recent work

Use arXiv and OpenReview where appropriate. Clearly distinguish:

```text
Published
arXiv Preprint
Under Review
```

Never imply that a preprint or submission is accepted.

### Priority 3 — Leading labs and companies

Monitor official research blogs, technical reports, and project pages from:

```text
OpenAI
Anthropic
Google DeepMind
Google Research
Meta AI
Microsoft Research
Stanford
Berkeley
MIT
CMU
Princeton
ByteDance
Alibaba
Tencent
```

### Priority 4 — Official artifacts

Use official repositories, benchmarks, datasets, evaluation suites, and code releases when they materially advance the research.

### Priority 5 — Community discovery signals

Researcher homepages, Google Scholar, Semantic Scholar, Hugging Face Papers, Papers with Code, GitHub Trending, X/Twitter, and newsletters may identify candidates only. Return to the paper, official page, official repository, or another primary source before verification.

## Workflow

Read `AGENTS.md`, `task.yaml`, the shared research skills, and `state/agent-memory-daily.json` before starting.

### Stage 1 — Candidate Discovery

Do not search only for `"Agent Memory"`. Expand across the direct and neighboring mechanisms listed in Scope, venue pages, lab pages, benchmark updates, code releases, and publication-status changes.

Target 10–30 valid candidates when the day supports it. A smaller pool is acceptable when high-quality additions are scarce; do not add noise to meet a count.

Record at least these fields for every candidate:

```text
title
authors
organization
date
source
url
status
topic
claimed_contribution
```

Maintain the candidate pool and rejection reasons in working notes or a sidecar artifact so the Top 5 remains auditable.

### Stage 2 — Independent Verification

Independently verify every serious candidate. Check:

1. Whether a primary source exists.
2. Whether the actual paper contribution matches the abstract, announcement, or promotion.
3. Whether it truly concerns Agent Memory at a mechanism level.
4. Whether `memory` is only a keyword.
5. Whether the method is merely existing RAG or retrieval under a new label.
6. Whether there is real methodological novelty.
7. Whether experiments support the central claim.
8. Whether the benchmark is appropriate.
9. Whether the work substantially duplicates prior work.
10. Whether an update is only a minor release.

Prioritize the abstract, introduction, method, experiments, conclusion, and official repository. Read deeper when those sections do not settle identity, contribution, or evidence quality.

For every item that survives, verify its canonical title, authors, date, primary URL, organization or venue, and status. Preserve uncertainty explicitly.

### Stage 3 — Ranking

Score each verified candidate internally:

```text
Research Significance      25
Novelty                    20
Agent Memory Relevance     20
Evidence Quality           15
Source Credibility         10
Recency                    10
------------------------------
Total                     100
```

Recency is only one dimension. Scientific value comes first; when quality is close, prefer the newer candidate.

Before selecting an item, answer:

```text
Why is this worth one of today's five positions?
```

Remove the item if the answer is not concrete and evidence-grounded.

### Stage 4 — Output

Write the final brief in the exact structure under Output Format. Avoid news-release language. Explain mechanism-level relevance and use primary-source links.

Save the brief to:

```text
outputs/agent-memory-daily/YYYY-MM-DD.md
```

Do not overwrite an existing completed brief without preserving the prior result.

### Stage 5 — Delivery

Persist the local output first. If all Feishu environment variables exist and delivery is enabled, use the shared Feishu delivery workflow and send the finalized brief. If variables are absent, mark `delivery_status=skipped`. If delivery fails, retain the output and mark `delivery_status=failed`.

## Selection Criteria

Select work with high scientific significance, methodological novelty, direct or paradigm-changing relevance to Agent Memory, credible evidence, and an authoritative source. A strong published work may outrank a newer preprint. A neighboring work belongs only when it materially changes how agent memory could be represented, learned, retrieved, consolidated, evaluated, or used.

## Rejection Criteria

Filter these by default:

```text
keyword-only matches
weak increments
simple prompt tricks
simple retrieval tuning
demos without evidence
duplicate publication
pure marketing
no primary source
minor GitHub releases
ordinary bug fixes
no substantive Agent Memory relevance
experiments that cannot support the claim
```

## Output Format

```markdown
# Agent Memory Daily Research Brief

Date: YYYY-MM-DD

## 今日一句话判断

一句话总结今天 Agent Memory 方向最重要的变化。

---

## 🥇 1. Title

Status:
Published / arXiv Preprint / Under Review / Technical Report / Benchmark / Code

Date:

Venue / Organization:

Primary Source:

### 核心贡献

2–4 句。

### 为什么与 Agent Memory 相关

说明机制级关联。

### 为什么进入今日 Top 5

说明最终 selection rationale。

### 我的判断

★★★★★

重点精读 / 建议阅读 / 跟踪即可

---

## 🥈 2. Title

使用相同字段。

---

## 🥉 3. Title

使用相同字段。

---

## 4. Title

使用相同字段。

---

## 5. Title

使用相同字段。

---

## 今日研究趋势

1.
2.
3.

---

## 最值得精读

Top 1:

Top 2:

原因：
```

## State

Use `state/agent-memory-daily.json` with at least:

```json
{
  "seen": {},
  "last_run": null,
  "baseline": null
}
```

Read state before selection to avoid repeating the same work across consecutive days. A previously seen item may reappear only for a material change, for example:

```text
arXiv → conference acceptance
paper → official code release
preprint → major revision
benchmark → major update
```

Label such an item `Update`; do not present it as a new paper. After output finalization, record stable identity, status, date last sent, and the update reason. Set `last_run` and establish `baseline` on the first successful full run.

## Failure Handling

- If research succeeds but Feishu fails, keep the report and record `delivery_status=failed`.
- If research fails, record the sanitized error and do not generate a fabricated Top 5.
- If there are too few high-quality new items, older but still important work may fill a position when clearly justified.
- Never invent papers or lower the evidence threshold merely to reach five items.
- Never write secrets, credentials, or full tokens to prompts, state, outputs, or logs.
- Do not use `OPENAI_API_KEY`; execute with the current Codex/ChatGPT runtime and available research tools.
