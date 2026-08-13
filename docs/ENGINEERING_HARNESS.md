# Engineering Harness

MindNook provides a one-command engineering verification harness:

```bash
python -m app.harness.runner
```

It creates a fresh temporary SQLite database, an in-memory Redis-compatible
short-term-memory store, a deterministic mock model provider, an ephemeral RAG
index, and temporary Excel output for every suite. It never reads or writes the
configured production database, Redis instance, or risk ledger.

## Suites

| Suite | Core verification |
|---|---|
| `risk-safety` | Explicit high-risk detection, psychological report persistence, backend metadata isolation, durable tool-job enqueue |
| `agent-routing` | `CHAT` / `CONSULT` / `RISK` routing, persisted Agent steps, collaboration actors |
| `standard-skills` | Required Skill validation, CONSULT/RISK selection, Workflow injection |
| `rag` | 100-case dataset coverage and Hit@1, Recall@3, MRR@3, nDCG@3, safety recall, duplicate-rate metrics |
| `api` | Liveness/readiness, login, authorization, public SSE, admin knowledge status/sources/rebuild APIs |
| `tool-queue` | Excel/case/alert DAG, idempotency, dependency, execution, rate limiting, dead letter |

Run selected suites by repeating `--suite`:

```bash
python -m app.harness.runner --suite risk-safety --suite tool-queue
```

Produce the complete machine-readable report:

```bash
python -m app.harness.runner --json
```

The process exits with code `0` only when every selected suite and check passes.

## RAG thresholds

The one-click harness uses a deterministic engineering regression floor so a
code or corpus regression fails quickly and reproducibly. Its JSON output also
includes `strict_acceptance_passed` and all strict checks from the standalone RAG
evaluation. The stricter release-quality gate remains:

```bash
python -m app.rag.evaluation --evaluate-answers --strict
```

Passing the engineering floor does not imply that the stricter RAG release gate
or human answer-quality review has passed.
