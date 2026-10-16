# Agent Application API Implementation Plan

> Execution: use superpowers:subagent-driven-development for independent publication, runtime and frontend work; integrate and review each component before final verification. No commits without a user request.

**Goal:** Deliver the reviewed application publishing design as a usable, tested single-process API feature.
**Architecture:** Immutable releases and deployments select configurations. Public invocations use client-scoped credentials, durable conversations, isolated checkpoints, idempotent runs and explicit cancellation. The existing private console and Agent ports remain compatible.
**Tech Stack:** Python 3.11, existing FastAPI/SQLAlchemy/SQLite, LangGraph SQLite checkpointer, React/TypeScript and native CSS.
**Spec:** `docs/architecture/application_api_publishing_design.md`.

## Constraints and Review Rulings

- Preserve the existing `issues/` directory and all unrelated changes. Work in the current non-main feature checkout; no commits, resets or external service calls.
- Knowledge uses latest committed revision per turn; released configuration and conversation release stay fixed.
- Profile configuration must match release on resume/admission; credential rotation is allowed without republishing.
- Public v0/v1 graph checkpoint wiring differs; v1 retrieval inner graph must not accumulate per-run persistent threads.
- Final public message comes from committed result, then done. No candidate/trace/tool argument exposure.
- Existing API envelope, request IDs and private SSE stay compatible. API secrets are write-only and validation input is redacted.
- Single backend process/worker; public nginx allowlist, private management. Public API disabled by default.
- Use failing tests first, then implement and run focused checks. Final checks: `make lint`, `make typecheck`, `make test`, frontend test/typecheck/build.

## Task 1: Publication and Access

Owned files: new `database/models/publishing.py`, `repositories/publishing.py`, `services/application_release_service.py`, `apis/v1/apps/publishing.py`, `apis/v1/publishing_schemas.py`, publication tests; application model and application response/create fields.

Interfaces: `ApplicationReleaseService(repository, applications)`; `get_publication(app_id)`, `publish(app_id, request)`, `set_enabled(app_id, request)`, `list_releases(app_id, limit, offset)`, `update(app_id, request)`. Credentials methods `create_client/list_clients/update_client/register_key/list_keys/revoke_key/authenticate`, returning typed schemas/principal. Expose repository `factory` for atomic run operations and typed release configs.

- [x] Write failing temporary-SQLite tests for publish/CAS/idempotence, immutable config, key lifecycle/no echo and profile mismatch.
- [x] Implement release/deployment/client/key models, request/response contracts and atomic repository operations.
- [x] Add application editing and management routes under `/api/v1/apps`; routes obtain `request.app.state.services.publishing`.
- [x] Run `uv run pytest tests/api_server/test_application_publishing.py` and resolve failures.

## Task 2: Public Runtime and Persistent Context

Owned files: new runtime schemas/models/repository/service/routes/tests, `sqlite_chat.py` console channel checks, `models/chat.py`, factory and outer graph checkpoint wiring. Main coordinator owns migrations, container, settings and application runtime factory integration.

Interfaces: `ApplicationInvocationService(publishing, applications, factory, settings)`; route registration via `apis/v1/runtime/api.py:router`, service available as `services.invocations`; `close()` closes worker/checkpoint resources. Application service exposes `release_runtime(config, checkpointer, knowledge_base_record)` for exact immutable execution config.

- [x] Write failing tests for authenticated JSON/SSE, app/client/user isolation, deduplication, cancel/timeout ownership and completed restart recovery.
- [x] Implement run/turn atomic admission and completion, result lookup, final-only public response mapping and terminal state fencing.
- [x] Inject persistent checkpointer at v0 main / v1 outer graph; keep CLI defaults; add explicit release generation parameters.
- [x] Hide API conversations from every private chat read/write/delete path.
- [x] Run focused runtime and Agent factory/graph tests.

## Task 3: Console Publishing Workflow

Owned files: frontend application feature, application API client/types, focused Vitest/CSS; coordinate shared `types.ts` and client contracts with Task 1.

- [x] Write failing UI tests for publish/disable, key register/no response echo, error/loading and cancellation states.
- [x] Add configuration editing, publication/client/key management and calls views; keep local key material in component memory only.
- [x] Use browser crypto for 32-byte secrets, clear on close, show usable secret only after successful registration.
- [x] Add endpoint/examples and responsive states; run Vitest/typecheck/build.

## Task 4: Integration, Migration and Index Safety

Owned files: migrations/model imports/container/settings/router/errors, ApplicationService release runtime, model reference protection, index coordination and gateway examples.

- [x] Write failing migration/secret-redaction/release-runtime/index coordination tests.
- [x] Add idempotent v8 upgrade, settings/env defaults and composition; recovery runs before accepting new requests.
- [x] Add release runtime construction using exact config, latest pinned snapshot, credential version and bounded lifetime.
- [x] Protect release references during Profile and knowledge-base edits/deletion; preserve current metadata boundaries.
- [x] Coordinate index mutation with snapshot-aware retrieval; test same-turn behavior during update.
- [x] Add public allowlist gateway config, deployment instructions and request examples.

## Task 5: Review and Acceptance

- [x] Review component diffs against the spec and tests, fix blocking findings and inspect final changes for secrets/unrelated files.
- [x] Run Python lint/typecheck/test and frontend test/typecheck/build; report precise remaining limitations.
- [x] Start an isolated local backend/frontend using temporary data and disabled external integrations; verify desktop/mobile layout and flows without real model calls.

## Progress Ledger

- Design self-review: complete; four findings corrected in the spec before implementation.
- Shared-file ruling: coordinator owns model registration/migrations/container/router/settings; workers own domain files and report signatures before integration.
- Task 1: complete, including dependency readiness and reference guards.
- Task 2: complete, including real runner regressions and real TCP replay verification.
- Task 3: complete; 49 frontend tests, typecheck and build pass.
- Task 4: complete; genuine v8 upgrade preserves messages and installs ownership checks.
- Task 5: complete; verified desktop and mobile flows with temporary data. `make test`: 617 passed, 7 external-integration tests skipped. `make typecheck`: zero errors. Frontend: 49 tests passed, typecheck/build passed (existing bundle-size warning remains).
- Lint limitation: all lint rules and task-owned formatting pass. Repository-wide formatting reports six pre-existing files outside this task; they were preserved.
- Deployment limitation: nginx is not installed locally, so the gateway example still requires deployment-environment validation. No real LLM, Embedding, MinerU or Milvus calls were made.
- Operational scope: one backend process, public API disabled by default, full final-answer SSE event, no time-range filtering or automatic audit-history deletion in the first version.
