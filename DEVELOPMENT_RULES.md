# Development Rules

Rules and conventions for working on the Podgen codebase. **Read before contributing.**

---

## Architecture Invariants

### State-by-IDs (hard rule)
LangGraph `PipelineState` holds **only ID references** — never texts, dicts, or domain objects.
Every pipeline node must:
1. Read `*_id` / `*_ids` from state
2. Load full data from PostgreSQL / Qdrant by those IDs
3. Write results back to PostgreSQL
4. Return only new `*_id` values to state

Violating this bloats checkpoints and breaks resumability.

### Lazy Loading
Texts (source content, evidence quotes, dialogue) are materialized **only in the node that needs them**, top-K. Never preload everything "just in case".

### Evidence-First
Every factual statement in generated dialogue must trace back to an `EvidenceSpan`. Claims are never derived from summaries — only from verbatim quotes.

### Non-Blocking Web
Deep web ingestion runs as Celery async tasks. **No pipeline node may block waiting for deep ingestion.** Act planners and screenwriter use only fast evidence.

### Governors
All loop/concurrency limits are in `app/config.py`. Never hardcode limits in pipeline nodes — always read from `settings.*`.

### Multi-Tenant Isolation
All data queries must filter by `project_id` and/or `user_id`. Never allow cross-tenant data access. ShowBible, Source, Claim, Episode — everything is scoped to a project. When accepting entity IDs from API input, always verify ownership.

### Observability-First (LangSmith)
- Tracing is implemented via `app.observability` helpers. Do not call LangSmith SDK directly from business modules.
- Every long-running execution path must have a root span:
  - Celery tasks (`worker.*`)
  - generation pipeline root (`pipeline.execute`)
  - ingestion pipeline root (`ingestion.run_ingestion`)
- LangGraph nodes must be wrapped with per-node spans (`pipeline.node.*`), carrying `phase`, `node`, `generation_run_id`, `project_id`.
- External operations should be traced with dedicated spans (`llm.*`, `research.tool.*`, `ingestion.*`) using concise input/output summaries.
- Trace payloads must be compact and safe:
  - No full raw source texts/transcripts in trace inputs/outputs.
  - Store sizes/counts/IDs and key status fields instead of large blobs.
- Tracing must never affect correctness:
  - Guard all trace writes via `safe_set_run(...)`.
  - Feature remains opt-in by env (`LANGSMITH_TRACING=true` + API key).

---

## LLM SDK & Models

### Prompt Design Standard
- Prompt contracts, language rules, and change checklist are defined in `PROMPT_RULES.md`.
- Any change to production prompts must follow that document and keep parsing/normalization deterministic.

### Anthropic SDK (`anthropic` Python package)
- Use `anthropic.AsyncAnthropic` for all calls (async-first).
- Models are defined as constants in `app/services/llm.py`:
  - **Sonnet**: `claude-sonnet-4-6` — reasoning, writing, verification, planning
  - **Haiku**: `claude-haiku-4-5-20251001` — classification, scoring, extraction, shownotes
- For structured output, append JSON instruction to system prompt and parse response manually. Do NOT rely on deprecated `response_format` — use the system prompt approach in `call_sonnet_structured` / `call_haiku_structured`.
- Always set `temperature` explicitly. Use low (0.0–0.3) for extraction/classification, medium (0.3–0.5) for structured reasoning, higher (0.5–0.7) for creative writing.

### OpenAI SDK (`openai` Python package)
- Used **only for embeddings**: `text-embedding-3-large`, 3072 dimensions.
- Use `openai.AsyncOpenAI` (async).
- Do NOT use OpenAI for chat/completion — all LLM work goes through Anthropic.

### ElevenLabs SDK (`elevenlabs` Python package)
- Used for TTS only.
- Voice IDs and settings come from `ShowBible.voice_profiles`.
- Use the sync client (TTS calls are CPU-bound and run in worker context).

### Updating Model IDs
When new model versions are released, update **only** the constants in `app/services/llm.py` (`SONNET_MODEL`, `HAIKU_MODEL`). All nodes import from there — never hardcode model IDs elsewhere.

---

## Dependencies & Deprecation Policy

### Python (backend)
- **Python 3.12+** required.
- Package manager: **uv** (not pip, not poetry).
- All dependencies in `pyproject.toml` with minimum version pins (`>=`), not exact pins.

### Key packages and known deprecation notes

| Package | Notes |
|---------|-------|
| `fastapi` | Use `Annotated[X, Depends(...)]` pattern, not old-style `= Depends(...)` |
| `sqlalchemy` | v2 async only. Use `mapped_column()`, not legacy `Column()`. Use `Mapped[]` type hints. |
| `pydantic` | v2 only. Use `model_validate()`, `model_dump()`, not deprecated v1 `.from_orm()`, `.dict()` |
| `pydantic-settings` | Separate package since Pydantic v2. Use `BaseSettings` from `pydantic_settings`, not `pydantic` |
| `langgraph` | v0.3+. Use `StateGraph`, not deprecated `MessageGraph` |
| `anthropic` | Use `client.messages.create()`, not legacy `client.completions` (removed). No `response_format` param — use system prompt for JSON |
| `openai` | Use `client.embeddings.create()`. The `engine` parameter is removed — use `model`. |
| `celery` | v5+. Use `app.task()` decorator, not deprecated `@celery.task` |
| `redis` | Use `redis.asyncio` for async, not deprecated `aioredis` standalone package |
| `httpx` | Preferred over `requests` for async HTTP. Use `AsyncClient` context manager. |
| `pyjwt` | Import as `jwt`, not `jose` (python-jose is abandoned) |
| `marker-pdf` | Actively maintained. If API changes, check `marker.convert` module. |
| `tiktoken` | Used for token counting in chunker. Model: `cl100k_base`. |
| `pydub` | Stable, no known deprecations. Requires `ffmpeg` system binary. |

### Frontend (Next.js)
- **Node.js 20+**, **npm** (not yarn/pnpm).
- **Next.js 15** with App Router. Do NOT use Pages Router.
- **React 19** — use hooks, no class components.
- **Tailwind CSS 4** — utility-first, no custom CSS unless absolutely necessary.
- **shadcn/ui** — copy-paste components in `src/components/ui/`. Use `class-variance-authority` for variants.
- **Zustand** for state, not Redux/Context.
- **ky** for HTTP (not axios, not fetch directly).
- TypeScript strict mode. No `any` types except in generated/third-party boundaries.

---

## Code Conventions

### Backend
- Async everywhere (FastAPI routes, SQLAlchemy sessions, LLM calls, Redis).
- Type hints on all function signatures.
- Use `StrEnum` for database enums, not raw strings.
- Pydantic schemas for API request/response, SQLAlchemy models for DB — never mix.
- SQL: parameterized queries only. No f-string SQL except for column/table names known at compile time.
- Imports: stdlib → third-party → app modules, separated by blank lines.

### Frontend
- `"use client"` directive only on components that need browser APIs or hooks.
- Server components by default.
- Colocate related files (page + its components in the same route folder).

### Pipeline Nodes
- Every node function signature: `async def node_name(state: PipelineState) -> dict`
- Return dict with only the state keys that changed.
- Publish real-time events via `publish_event()` for user-visible progress.
- Use `async_session_factory()` for DB access inside nodes (not the FastAPI dependency injection).
- Wrap external calls (LLM, web fetch) in try/except — a single failed evidence extraction should not crash the pipeline.
- Keep node tracing metadata accurate:
  - `phase` and `node` tags must match the actual graph node.
  - include `generation_run_id` and `project_id` in trace context.

### Testing
- Use `pytest` + `pytest-asyncio`.
- Test files mirror source structure: `tests/test_api/`, `tests/test_pipeline/`, `tests/test_ingestion/`.
- For API tests, use `httpx.AsyncClient` with the FastAPI app.

### Linting
- **ruff** for linting and formatting. Config in `pyproject.toml`.
- Line length: 120.
- Target: Python 3.12.

---

## Security Rules

### SSRF Protection
All outbound HTTP requests (web ingestion, Jina Reader, Tavily) must pass through `_check_ssrf()` from `app/ingestion/web.py`. Denylist: private IPs (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), link-local, cloud metadata (169.254.169.254).

### File Size Limits
Max 50MB for downloaded files (`settings.web_max_file_size_mb`). Enforced in web ingestion and deep ingestion.

### Auth
- JWT Bearer token on all API endpoints (except health, dev-login, presets).
- WebSocket: JWT passed as query param `?token=`, verified before accept.
- Always verify resource ownership (project belongs to user, generation run belongs to user's project).

### Secrets
- All API keys in `.env`, never in code.
- `.env` is in `.gitignore`.
- Use `pydantic-settings` `BaseSettings` to load env vars.

---

## Git & Workflow

- Main branch: `main`.
- Feature branches: `feat/<description>`.
- Commits: imperative mood, concise ("Add show bible validation", not "Added some stuff").
- No secrets, no `.env`, no `node_modules`, no `__pycache__` in commits.
- Run `ruff check` before committing Python changes.
