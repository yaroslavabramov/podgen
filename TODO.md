# Podgen — Progress Tracker

## Legend
- [x] Done
- [~] Partial (scaffolded, needs refinement)
- [ ] Not started

---

## Этап 1: Foundation

- [x] Init monorepo: `backend/` (uv + FastAPI), `frontend/` (Next.js + shadcn/ui)
- [x] Docker Compose: postgres, redis, qdrant, minio, api, worker, frontend
- [x] Database models (User, Project, Source, ShowBible, Chunk, ContentMap, EvidenceSpan, Claim, ClaimEvidence, ConflictBrief, GenerationRun, EpisodeManifest, ScriptSegment, Episode)
- [x] Alembic config (env.py, alembic.ini)
- [ ] Alembic initial migration (`alembic revision --autogenerate`)
- [x] FastAPI app factory + Pydantic Settings config
- [x] Auth: JWT middleware + dev-login endpoint
- [ ] Auth: OAuth (NextAuth v5) — deferred, dev-login sufficient for now
- [x] MinIO service (upload, download, presigned URLs)
- [x] CRUD API: projects, sources, show-bibles, episodes, generations

## Этап 2: Ingestion (Phase 1)

- [x] PDF ingestion (Marker)
- [~] Web ingestion (Jina Reader + httpx fallback; FireCrawl/Playwright cascade not implemented)
- [x] Video/Audio ingestion (Deepgram)
- [x] Structural chunker (structure-first, then normalize)
- [x] Embeddings service (OpenAI → Qdrant)
- [x] Canonicalizer (dedup, reliability classification)
- [x] Scout (ContentMap generation)
- [x] Celery setup + ingestion tasks
- [~] Frontend: Source uploader (basic upload, no tus resumable)

## Этап 3: Show Setup + LangGraph Core (Phases 0, 2-4)

- [x] LangGraph state definition (PipelineState, IDs only)
- [x] Graph assembly (graph.py with all nodes and edges)
- [~] LangGraph checkpointing — graph compiles without checkpointer, resume not functional
- [x] Phase 0: Show Configurator (ShowBible generation + presets)
- [x] Phase 2: Decomposer + Duration Fill Strategy
- [x] Phase 3.1: Hunter (closed-book retrieval, hybrid RRF)
- [x] Phase 3.2A: Librarian Fast (web search with Tavily)
- [x] Phase 3.2B: Deep ingestion (Celery async)
- [x] Phase 4.1: Claim Builder
- [x] Phase 4.2: Verifier
- [x] Phase 4.3: Conflict Resolver
- [x] Real-time events: Redis pub/sub → WebSocket
- [~] Frontend: ShowBible editor (basic, no voice selector UI)

## Этап 4: Planning & Writing (Phases 5-6)

- [x] Phase 5.1: Showrunner (act planning with DISPUTED quality gate)
- [x] Phase 5.2: Act Planners
- [x] Phase 5.3: Integrator
- [x] Phase 6.1: Screenwriter
- [x] Phase 6.2: Writer Revolt loop
- [x] Phase 6.3: Citation Auditor

## Этап 5: Audio (Phase 7)

- [x] ElevenLabs TTS integration
- [x] Audio processing (pydub/ffmpeg, LUFS normalization)
- [x] Shownotes generator
- [x] Episode CRUD API + audio streaming

## Этап 6: Frontend Completion

- [x] Project list page
- [x] Project detail page (sources tab, show bible tab, episodes tab)
- [~] Generation config page (basic, cost estimate simplified)
- [x] Real-time progress UI (phase stepper, event feed)
- [x] Episode player (HTML5 audio + synced transcript)
- [x] Login page (dev-login)
- [ ] Episodes list page (missing)
- [ ] Shownotes viewer component (missing)
- [ ] Cost estimate component (inline in generate page, not standalone)
- [ ] i18n (EN/RU) with next-intl
- [ ] wavesurfer.js waveform in player (using basic HTML5 audio)
- [ ] tus resumable uploads

## Этап 7: Production Quality

- [ ] End-to-end tests
- [ ] Token & Cost Governor enforcement (limits exist in config, enforcement partial)
- [ ] Error handling + retry strategies
- [~] Generation resume (API endpoint exists, checkpointer not wired)
- [ ] Structured logging + monitoring
- [ ] Cost tracking (actual vs estimated)
- [ ] Temp source cleanup tied to generation_run (currently age-based only)

---

## Known Issues (from code review)

See `REVIEW_FINDINGS.md` for detailed findings. Key fixes applied:

- [x] **P0**: `is_safe_url` import mismatch in tools.py → fixed to use `_check_ssrf`
- [x] **P0**: `EpisodeManifest.duration_fill_layers` type mismatch (UUID vs String) → fixed to `ARRAY(String)`
- [x] **P0**: Presets with fake project_id FK → fixed to nullable project_id for presets
- [x] **P1**: WebSocket without ownership check → added JWT + ownership verification
- [x] **P1**: `GET /api/projects/{id}` missing sources/show_bible → added to response
- [x] **P1**: Source missing `generation_run_id` for temp lifecycle → added field
- [x] **P1**: Graph quality loops stubbed → implemented conditional logic

Deferred (by design):
- **P1**: Full LangGraph checkpointing (requires production PostgreSQL saver setup)
- **P2**: OAuth endpoints (using dev-login for development)
- **P2**: Missing frontend pages (episodes list, register) — low priority
- **P2**: Full web cascade (FireCrawl + Playwright) — Jina + httpx sufficient for MVP
- **P2**: tus uploads, wavesurfer.js — nice-to-have UX features
