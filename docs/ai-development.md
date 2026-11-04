# AI-assisted development

Codex assisted with this assignment's design, code, tests, and documentation. The user supplied the requirements and directed the work phase by phase. This document records that use; it is not a claim that all generated suggestions were correct or that the recommended timebox was measured.

## How AI was used

The design was based on the assignment and actual sample files. The user then requested a more general architecture, explicit identifier namespaces, authority/freshness-based conflict handling instead of file-format precedence, and clearer separation of fixtures from core rules. Those decisions guided implementation.

Codex produced SQLAlchemy models and validation contracts, deterministic adapters, identity/evidence services, upload handling, OpenRouter extraction, bounded query plans, query execution, and tests. Implementation proceeded through the requested phases, with test results and deviations reported after each. The user installed missing dependencies from their terminal when the sandbox could not reach the package index.

AI assistance also helped discover defects through testing. For example, Phase 7's HTTP fault injection showed that unexpected 500 responses had a body request ID without the matching header. The shared error-response helper was corrected and the rollback/recovery tests passed. That test finding is distinct from the application using AI at runtime.

Runtime AI has two narrow roles: extracting unfamiliar input and interpreting questions as plans. Deterministic code owns entity identity, conflict selection, relationship validation, and SQL execution. The default demo supplies explicit mock plans, so it does not demonstrate a model independently understanding the seven questions.

## Sample session excerpts

The following are genuine excerpts from the visible user/Codex conversation. They are a curated sample, not a complete raw chat export. Omitted material is indicated; prose outside the quotes summarizes the work. No credentials, uploaded secret contents, or private machine paths are included.

**User, design revision:**

> File format does not determine which source is more trustworthy.

**User, design revision:**

> The system should detect the conflict rather than automatically merging them.

These instructions led to configured attribute authority, comparable observation freshness, retained conflicting evidence, and conservative identity resolution.

**User, Phase 6:**

> implement phase 6

**Codex, Phase 6 result (excerpt):**

> Implemented Phase 6:
>
> - `POST /ask` returns grounded answers through validated plans and safe SQLAlchemy execution.
> - Exact reference lookup supports configured aliases and duplicate-name clarification.
> - Filtering, counts, pagination, and two-hop traversal preserve same-user semantics.
> - Warnings disclose unknown/conflicting fields and missing relationship evidence.

[Remaining Phase 6 response omitted. The reported suite result at that point was 404 passing tests.]

**User, Phase 7:**

> now implement phase 7: integration testing

**Codex, Phase 7 result (excerpt):**

> Implemented Phase 7 with **31 new integration tests** covering:
>
> - Mixed upload formats, fixture ordering, and replay.
> - Persistence across restarts and concurrent ingestion.
> - AI extraction through grounded `/ask` answers.
> - Quarantine, rollback, database locks, and provider failure recovery.
>
> Fixed one discovered bug: unexpected 500 responses now include the matching `X-Request-ID` header.

[Remaining Phase 7 response omitted. The reported full suite result was 435 passing tests.]

**User, Phase 8:**

> now implement phase 8

**Codex, Phase 8 plan (excerpt):**

> Phase 8 needs a runnable demo and an AI-development record alongside the README updates. I’ll add a demo that uses a temporary database and the real API/services, with clearly labeled mocked plans by default and an explicit `--live` option. I’ll also include genuine excerpts from this conversation and document what remains unverified.

## Verification and responsibility

Unit and integration tests, actual offline-demo responses, and the code are the evidence for implemented behavior. Passing mocked-provider tests does not establish live provider compatibility or model accuracy. OpenRouter connection attempts from this sandbox have failed; live checks remain available explicitly, rather than being hidden behind a mock fallback.

The development environment used Python 3.14.2. The declared minimum is Python 3.12, but that runtime and a completely fresh dependency download have not been exercised here. Dependency ranges, SQLite write serialization, bounded query vocabulary, and conservative unresolved identity are deliberate prototype tradeoffs documented in the README/design.

Before submission, the author should review the code and this record, run the demo/tests, and confirm the private repository or ZIP excludes keys, local databases, and environment files. This excerpt is the sample coding-session deliverable; it is not a fabricated full transcript or a transcript of live model reasoning.
