# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Podgen is a multi-tenant SaaS for automated AI podcast generation (15-90+ min, 2 hosts). Users upload sources, configure a "Show Bible" (host personas, style, audience), and the system runs an 8-phase LangGraph pipeline to produce a full podcast episode with audio, transcript, and shownotes.

## Architecture

**Monorepo:** `backend/` (Python/FastAPI) + `frontend/` (Next.js/TypeScript)

**Backend stack:** FastAPI, LangGraph (state machines), Celery+Redis (async tasks), PostgreSQL+SQLAlchemy 2 (async), Qdrant (vector search), MinIO (file storage), ElevenLabs (TTS)

**Frontend stack:** Next.js 15 (App Router), TypeScript, Tailwind CSS 4, shadcn/ui, Zustand, WebSocket for real-time

## Commands

### Development Setup
```bash
# Start all infrastructure (postgres, redis, qdrant, minio)
docker compose up -d postgres redis qdrant minio

# Backend
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000

# Celery worker
uv run celery -A app.workers.celery_app worker -l info

# Frontend
cd frontend
npm install
npm run dev
```

### Testing
```bash
# Backend tests
cd backend
uv run pytest
uv run pytest tests/test_api/  # specific directory
uv run pytest tests/test_api/test_projects.py -k "test_create"  # specific test

# Lint
uv run ruff check .
uv run ruff format .
```

### Database Migrations
```bash
cd backend
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
uv run alembic downgrade -1
```

### Full Stack (Docker)
```bash
docker compose up  # all services including api, worker, frontend
```

## Key Architecture Patterns

### State-by-IDs (Critical Rule)
LangGraph state (`backend/app/pipeline/state.py`) contains ONLY ID references — never texts or objects. Each pipeline node:
1. Takes `*_id` from state
2. Loads data from PostgreSQL/Qdrant
3. Writes results to DB
4. Returns only new `*_id` values to state

### Pipeline Phases (0-7)
`backend/app/pipeline/` — 8 phases orchestrated by LangGraph (`graph.py`):
- Phase 0: Show Bible setup (`phase0_setup/`)
- Phase 2: Query decomposition → research tree (`phase2_decomposition/`)
- Phase 3: Targeted research — closed-book + open-web (`phase3_research/`)
- Phase 4: Claim building + fact-checking (`phase4_claims/`)
- Phase 5: Hierarchical episode planning (`phase5_planning/`)
- Phase 6: Dialogue writing + citation audit (`phase6_writing/`)
- Phase 7: TTS audio generation + shownotes (`phase7_audio/`)

Phase 1 (Ingestion) runs separately via Celery when sources are uploaded (`backend/app/ingestion/`).

### Chunker (`backend/app/ingestion/chunker.py`)
Structure-first text chunking: infers headings from plain text when no markdown `##`/`###` present.
- **Heading hierarchy**: `_SECTION_LABEL_RE` recognizes `chapter`, `section`, `part`, `annex`, `appendix`, `book`, `title`. `article` is intentionally excluded — articles are document content, not structural headings.
- **Child-heading transparency**: `_has_supporting_body()` skips child headings (level > current) when looking for body text. Only sibling/parent headings (level ≤ current) stop the lookahead. This ensures parent headings like "Chapter IV" aren't discarded when followed directly by "Section 1" without intermediate text.
- **Heading path propagation**: `_split_by_inferred_headings()` updates `current_h2` before checking for empty body, so bodyless parent headings correctly propagate to child sections' `heading_path`.
- **Anti-TOC density guard**: if heading candidates exceed 35% of non-empty lines, all inferred headings are rejected (likely a table of contents).

### Governors
Rate limits defined in `backend/app/config.py`: max fast web per act (5), max deep ingestion concurrent (3), max writer revolt per act (4), max writer revolt per scene (2), max verifier research loops (2).

### TempSource Discipline
Fast web evidence creates `Source(is_temporary=true)`. Claims relying only on temporary sources are limited to OBSERVED/UNVERIFIED verification with mandatory hedging in dialogue.

### Real-time Events
Pipeline nodes → Redis pub/sub → FastAPI WebSocket (`backend/app/api/ws.py`) → frontend Zustand store (`frontend/src/stores/generation-store.ts`).

## Database Models
`backend/app/models/` — 12 models: User, Project, ShowBible, Source, Chunk, ContentMap, EvidenceSpan, Claim+ClaimEvidence, ConflictBrief, GenerationRun, EpisodeManifest, ScriptSegment, Episode.

## API Routes
All prefixed `/api/`. Routers in `backend/app/api/`: projects, show_bibles, sources, episodes, generations, ws. Auth via JWT Bearer token (`backend/app/api/deps.py`).

## LLM Usage
- **Claude Sonnet 4** (`claude-sonnet-4-6`): reasoning and writing tasks (decomposer, planners, screenwriter, verifier, auditor)
- **Claude Haiku** (`claude-haiku-4-5-20251001`): classification/light tasks (scout, canonicalizer, engagement eval, shownotes)
- **OpenAI text-embedding-3-large**: embeddings (3072 dims)
- Client helpers in `backend/app/services/llm.py` — default `max_tokens=16384` for structured calls
- All pipeline nodes use `parse_llm_json()` from `backend/app/pipeline/llm_json.py` (handles markdown fences, truncated output, empty responses)

## Runtime Notes
- **Python 3.13+**: requires `audioop-lts` package (pydub dependency, `audioop` removed in 3.13)
- **qdrant-client 1.17+**: uses `query_points()` not `search()` (old API removed)
- **Frontend API calls**: go directly to backend (`NEXT_PUBLIC_API_URL=http://localhost:8000`), not through Next.js rewrites (rewrites don't forward Authorization header on POST)
- **Celery workers** don't auto-reload code — restart worker after code changes

## Language
All pipeline artifacts (claims, beat sheets, dialogue) are in the podcast's target language (EN or RU). EvidenceSpan verbatim quotes stay in the source's original language.
