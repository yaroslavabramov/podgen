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
| Ingestion | Marker (PDF), Jina Reader (web), Deepgram (audio/video) |

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
| POST | `/api/projects/{id}/generate` | Start podcast generation |
| GET | `/api/generations/{id}` | Generation status |
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

## Documentation

- **PIPELINE.md** — Detailed pipeline architecture and design decisions
- **IMPLEMENTATION.md** — Full implementation specification with data models
- **CLAUDE.md** — AI assistant guidance for working with this codebase
