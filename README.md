# Podgen

AI-powered podcast generation platform. Upload sources (PDFs, web articles, videos), configure your show style, and generate full-length podcast episodes (15–90+ min) with two AI hosts, complete with audio, transcript, and shownotes.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      Frontend (Next.js 15)               │
│  App Router • Tailwind 4 • shadcn/ui • Zustand • WS     │
└──────────┬────────────────────────────┬──────────────────┘
           │ REST API                   │ WebSocket
┌──────────▼────────────────────────────▼──────────────────┐
│                   Backend (FastAPI)                       │
│  JWT Auth • CRUD APIs • WebSocket relay                  │
├──────────────────────────────────────────────────────────┤
│              LangGraph Pipeline (8 phases)                │
│  0. Show Setup → 2. Decomposition → 3. Research →        │
│  4. Claims → 5. Planning → 6. Writing → 7. Audio         │
├──────────────────────────────────────────────────────────┤
│                  Celery Workers                           │
│  Source ingestion • Deep web ingestion • Generation       │
└──┬──────┬──────┬──────┬──────┬───────────────────────────┘
   │      │      │      │      │
 Postgres Redis Qdrant MinIO  LLM APIs
 (data)  (cache) (vec) (files) (Anthropic, OpenAI, ElevenLabs)
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI, Python 3.12+ |
| Pipeline | LangGraph (state machines), Celery 5 |
| Database | PostgreSQL 16, SQLAlchemy 2 (async), Alembic |
| Vector DB | Qdrant |
| Cache/Queue | Redis 7 |
| File Storage | MinIO (S3-compatible) |
| LLM | Claude Sonnet 4, Claude Haiku, OpenAI embeddings |
| TTS | ElevenLabs |
| Frontend | Next.js 15, TypeScript, Tailwind CSS 4, shadcn/ui |
| State Management | Zustand |
| Ingestion | Marker (PDF), MediaWiki/trafilatura/Jina (web), Deepgram (audio/video) |

## Quick Start

### Prerequisites
- Docker & Docker Compose
- Python 3.12+ with [uv](https://github.com/astral-sh/uv)
- Node.js 20+

### 1. Clone & configure

```bash
cp .env.example .env
# Edit .env — add your API keys (Anthropic, OpenAI, ElevenLabs, etc.)
```

### 2. Start infrastructure

```bash
docker compose up -d postgres redis qdrant minio
```

### 3. Backend

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

### 4. Celery worker (separate terminal)

```bash
cd backend
uv run celery -A app.workers.celery_app worker -l info
```

### 5. Frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev
```

App runs at http://localhost:3000, API at http://localhost:8000.

### Full Docker (alternative)

```bash
docker compose up
```

Starts all 8 services: postgres, redis, qdrant, minio, api, worker, beat, frontend.

## Pipeline Overview

The podcast generation pipeline has 8 phases orchestrated by LangGraph:

| Phase | Name | Description |
|-------|------|-------------|
| 0 | Show Setup | Generate/load ShowBible (host personas, style, audience) |
| 1 | Ingestion | Extract text from sources, chunk, embed (runs on upload via Celery) |
| 2 | Decomposition | Build research tree, determine duration fill strategy |
| 3 | Research | Closed-book retrieval + open-web search (fast + deep async) |
| 4 | Claims | Build claims from evidence, verify, resolve conflicts |
| 5 | Planning | Showrunner plans acts, act planners create beat sheets |
| 6 | Writing | Screenwriter generates dialogue, citation audit, writer revolt loop |
| 7 | Audio | TTS generation, audio processing, shownotes |

Key principle: **State-by-IDs** — LangGraph state holds only ID references, never full texts. Each node loads from DB and writes back.

Phase 4 now uses explicit lineage and canonical evidence semantics:
- `EvidenceSpan` is canonical quote-level evidence
- `EvidenceProvenance` stores which question/branch/run found that evidence, its context-specific `asset_type`, and whether it was `direct` or `contextual` for that question
- claim building batches by research branch, not by flat evidence order
- verifier-triggered fast-web loops now rebuild only impacted claim groups instead of re-running claim generation over the full evidence set
- fast-web now budgets by processed gap attempts, not by evidence-item count; initial Hunter gap-fill and verifier corroboration loops keep separate budgets/counters
- fast-web uses the shared lightweight web extraction stack (`mediawiki -> trafilatura -> jina`) plus `clean_web_text(...)`, builds relevance excerpt windows, and skips already-ingested fully known URLs before fetch/extract
- fast-web query generation is now universal LLM query synthesis for all languages and both modes; raw question/claim strings are no longer sent as final search queries
- verifier-loop fast-web now runs in explicit `claim_corroboration` mode: query synthesis, reranking, and excerpt filtering are claim-anchor-first, question-level asset hints stay only as background extractor context, extractor payload uses compact corroboration goals instead of duplicating verbose `search_objective`, and verifier-mode extracted items are no longer rejected by question-level `target_missing_assets`
- fast-web step-stop now aborts the node instead of being swallowed as a per-query failure, and fast-web debug/accounting distinguishes accepted extractor items from truly new canonical evidence / provenance links; realtime `evidence_found` reflects run-usable linked evidence (new canonical evidence or new provenance links), not only brand-new quotes
- duplicate claims are collapsed before verification
- verifier counts independent sources via canonical source identity and caps temp-only evidence at `OBSERVED`
- verifier sees full supporting quotes and splits LLM batches by char budget instead of truncating every quote to 300 chars
- verifier is incremental after `dedupe_claims`: it only re-checks changed claims, stores `verification_fingerprint` on `Claim`, and clears rebuild/dedupe coordination metadata after each pass
- incremental verifier scope now honors the full merged `claim_ids` set from overlapping research gaps, not just a lead claim
- targeted rebuild now reuses prior verification state/fingerprint for semantically unchanged rebuilt claims and assigns rebuilt claims to the best-fitting branch/question using support-lineage semantics instead of flat provenance only
- targeted rebuild scope now expands merged verifier gaps across all participating `claim_ids` and branch/question contexts before rebuilding
- claim builder now budgets against real prompt-quote lengths and uses adaptive long-quote packing instead of a blind `[:700]` prefix cut
- claim builder prompt now optimizes for a compact writer-usable backbone: it prefers fewer stronger claims, merges evidence by proposition core, and avoids `one evidence span -> one micro-claim`
- Hunter extraction now retries failed/invalid extraction passes with a narrower payload and tracks `expected_evidence` hits explicitly in debug/summary output
- fast-web page retention now filters bibliography-like windows, records explicit drop reasons, and uses authority-aware retention plus weak-source-family penalties before extractor spend
- planning payloads now carry claim-level narrative/support signals, and `act_planner` no longer depends on a flat lossy `evidence[:20]` bag
- Hunter retrieval and Claim Builder instructions are intent-aware for `debate` / `compare` / `case_study`, with stricter guardrails against claim overshoot from partial or contextual evidence
- showrunner caps per-act claim density so planning acts behave like curated story layers instead of near-complete research dumps, but no longer invents synthetic numeric conflicts from nearby claims
- density capping preserves disputed/conflict claims so planning cannot silently drop debate moments before validation
- preserved disputed/conflict claims still remain inside a bounded act cap; conflict-heavy acts stay curated instead of bypassing density control entirely
- showrunner now plans explicit act story contracts (`story_role`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`, `focus_branch_ids`) so planning works from editorial intent and act movement, not only from grouped claim IDs
- planning payloads now carry `output_language`, so showrunner and act planner keep human-readable output in the podcast language instead of drifting into English
- act planner now uses the act story contract, branch-focus summary, scene-count guidance, and claim-first support bundles to build compact beat cards instead of long pseudo-script scenes; it asks the model to declare a short `scene_strategy`, explicitly warns against the default `hook -> explanation -> case_study -> takeaway` skeleton, treats `case_study` as optional rather than mandatory, and deterministically reuses bundled claim evidence before falling back to vector-search rescue
- integrator now uses a stricter structured-output contract with retry and debug dumps, so long transition diagnostics do not silently truncate into a no-op at `2048` tokens
- screenwriter now receives a trimmed scene-writing payload (`act_title`, `previous_scene`, `already_covered_claims`, `transition_note`) instead of the full act contract, so it keeps dialogue in the podcast language while avoiding scene-to-scene re-explanations of already covered material
- screenwriter now writes per-scene JSON debug dumps (`tmp/debug/screenwriter`) with input payload, attempt history, raw/parsed LLM output, and final dialogue, so phase 6 regressions can be inspected directly from artifacts instead of only from final DB rows
- when `WEB_EXTRACTOR_DEBUG_DUMP_DIR` is set, Hunter / Claim Builder / Verifier / Showrunner / Act Planner write JSON debug dumps for run analysis

## Runtime Reliability Notes (2026-02-23)

- Celery workers use a dedicated DB session factory with `NullPool` (`backend/app/workers/worker_db.py`) to avoid asyncpg event-loop binding issues in worker processes.
- All pipeline modules (`backend/app/pipeline/**`) use the worker session factory when running inside generation worker tasks.
- Source ingestion lifecycle is `pending -> enqueued -> processing -> completed|failed`.
- `ingest_source` retries transient SQLAlchemy DB errors and marks source as `failed` after retry exhaustion (prevents forever-`pending` sources).
- On successful ingestion, stale `metadata.error` is cleared.
- ShowBible generation has no silent fallback defaults:
  - LLM call / JSON parse / payload validation errors raise `ShowBibleGenerationError`
  - `POST /api/projects/{id}/show-bible/generate` returns `502` with error details
- After backend worker code changes, restart Celery workers to load updated task modules and runtime behavior.
- PDF model cold-start: prewarm marker models once to avoid long first-PDF latency in worker.
  - Run from `backend/` venv:
    - `python -c "from app.ingestion.pdf import prewarm_marker_models; prewarm_marker_models(); print('marker models ready')"`
  - Cache location (macOS): `~/Library/Caches/datalab/models`

## Project Structure

```
podgen/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers (projects, sources, episodes, etc.)
│   │   ├── models/       # SQLAlchemy ORM models (12 models)
│   │   ├── schemas/      # Pydantic request/response schemas
│   │   ├── services/     # Storage, embeddings, LLM, cost estimation
│   │   ├── ingestion/    # PDF, web, video extraction + chunking
│   │   ├── pipeline/     # LangGraph phases 0-7
│   │   ├── workers/      # Celery tasks
│   │   └── realtime/     # WebSocket events + Redis pub/sub
│   ├── alembic/          # Database migrations
│   └── tests/
├── frontend/
│   └── src/
│       ├── app/          # Next.js App Router pages
│       ├── components/   # UI components (shadcn/ui + custom)
│       ├── stores/       # Zustand state management
│       └── lib/          # API client, auth, utilities
├── docker-compose.yml
├── PIPELINE.md           # Detailed pipeline architecture (v2.3)
└── IMPLEMENTATION.md     # Full implementation specification
```

## API

All endpoints prefixed with `/api/`. Auth via JWT Bearer token.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/dev-login` | Dev login (returns JWT) |
| GET | `/api/projects` | List user projects |
| POST | `/api/projects` | Create project |
| GET | `/api/projects/{id}` | Project details with sources & show bible |
| POST | `/api/projects/{id}/sources/upload` | Upload source file |
| POST | `/api/projects/{id}/sources/url` | Add URL source |
| POST | `/api/projects/{id}/show-bible` | Create/update show bible |
| POST | `/api/projects/{id}/generate` | Start generation (`execution_mode=full|step`) |
| GET | `/api/generations/{id}` | Generation status |
| POST | `/api/generations/{id}/steps/next` | Run exactly one next pipeline step (step mode only) |
| POST | `/api/generations/{id}/steps/rerun` | Rerun a selected completed pipeline step (defaults to the last executed step; step mode only) |
| POST | `/api/generations/{id}/steps/stop` | Request stop for running/queued step action (step mode only) |
| POST | `/api/generations/{id}/resume` | Resume failed full-mode run from checkpoint |
| WS | `/api/ws/generations/{id}` | Real-time generation events |
| GET | `/api/episodes/{id}` | Episode details |
| GET | `/api/episodes/{id}/audio` | Stream episode audio |

## Environment Variables

See `.env.example` for all required variables. Key ones:
- `ANTHROPIC_API_KEY` — Claude API for reasoning/writing
- `OPENAI_API_KEY` — Embeddings (text-embedding-3-large)
- `ELEVENLABS_API_KEY` — TTS voice synthesis
- `TAVILY_API_KEY` — Web search
- `JINA_API_KEY` — Web content extraction
- `DEEPGRAM_API_KEY` — Audio/video transcription
- `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` — optional end-to-end tracing for workers, LangGraph nodes, LLM calls, and research tools

## LangSmith Tracing

1. Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, and (optionally) `LANGSMITH_PROJECT`.
2. Restart API and Celery workers.
3. Open your LangSmith project and inspect traces:
   - root task spans: `worker.run_generation_pipeline`, `worker.ingest_source`, `worker.deep_ingest_url`
   - pipeline spans: `pipeline.execute`, `pipeline.node.*`
   - model spans: `llm.call_*`, `llm.get_embeddings` (+ wrapped provider calls)
   - research/ingestion tool spans: `research.tool.*`, `ingestion.*`

## Step-by-Step Debug Mode

Use this mode to debug one pipeline step at a time and reduce LLM/TTS spend during iteration.

1. In generation UI, enable `Step-by-step debug mode` before starting a run.
2. The run is created in `execution_mode=step` and does not auto-run the full graph.
3. Use:
   - `Run Next Step` to execute one node
   - `Rerun Selected Step` to re-execute a specific completed node from its checkpoint position
   - `Stop Current Step` to request cooperative stop of the current step action
4. Progress/events continue via WebSocket (`generation_paused` is emitted after each step, including stop acknowledgements).
5. `rerun` is available only after at least one completed step with checkpoint metadata.

## Documentation

- **PIPELINE.md** — Detailed pipeline architecture and design decisions
- **IMPLEMENTATION.md** — Full implementation specification with data models
- **PIPELINE_IO_MAP.md** — Code-level phase-by-phase I/O map (inputs/outputs/tools/events) + docs-vs-code diff
- **CLAUDE.md** — AI assistant guidance for working with this codebase
