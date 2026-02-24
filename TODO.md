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
- [x] Alembic initial migration (manual rewrite for cyclic FK: generation_runs ↔ show_bibles)
- [x] FastAPI app factory + Pydantic Settings config
- [x] Auth: JWT middleware + dev-login endpoint
- [ ] Auth: OAuth (NextAuth v5) — deferred, dev-login sufficient for now
- [x] MinIO service (upload, download, presigned URLs)
- [x] CRUD API: projects, sources, show-bibles, episodes, generations

## Этап 2: Ingestion (Phase 1)

- [x] PDF ingestion (Marker)
- [~] Web ingestion (Jina Reader + httpx fallback; FireCrawl/Playwright cascade not implemented)
- [x] Video/Audio ingestion (Deepgram)
- [x] Text normalization layer (Unicode NFC, whitespace, PDF hyphenation/boilerplate/page numbers)
- [x] Structural chunker (structure-first, then normalize)
- [x] Embeddings service (OpenAI → Qdrant)
- [x] Canonicalizer (dedup, reliability classification)
- [x] Scout (ContentMap generation)
- [x] Celery setup + ingestion tasks
- [x] Frontend: Source uploader (tus resumable + fallback upload)

## Этап 3: Show Setup + LangGraph Core (Phases 0, 2-4)

- [x] LangGraph state definition (PipelineState, IDs only)
- [x] Graph assembly (graph.py with all nodes and edges)
- [x] LangGraph checkpointing — Async Postgres checkpointer wired (`thread_id` persisted per run), resume path uses saved checkpoints
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

- [x] ElevenLabs TTS integration (eleven_v3 model)
- [x] Audio processing (pydub/ffmpeg, LUFS normalization) — requires `audioop-lts` on Python 3.13+
- [x] Shownotes generator
- [x] Episode CRUD API + audio streaming

## Этап 6: Frontend Completion

- [x] Project list page
- [x] Project detail page (sources tab, show bible tab, episodes tab)
- [x] Generation config page (voices + ShowBible summary + cost estimate)
- [x] Real-time progress UI (phase stepper, event feed)
- [x] Episode player (HTML5 audio + synced transcript)
- [x] Login page (dev-login)
- [x] Episodes list page (`/episodes`)
- [x] Shownotes viewer component (structured renderer)
- [x] Cost estimate component (standalone `CostEstimateCard`)
- [x] i18n (EN/RU) with locale switcher + dictionaries
- [x] wavesurfer.js waveform in player
- [x] tus resumable uploads

## Этап 7: Production Quality

- [ ] End-to-end tests
- [ ] Token & Cost Governor enforcement (limits exist in config, enforcement partial)
- [~] Error handling + retry strategies (ingestion DB retries + explicit ShowBible generation errors done; full coverage pending)
- [x] Generation resume (continues from last checkpoint; no full pipeline rerun on retry)
- [~] Structured logging + monitoring (LangSmith tracing integrated; dashboards/alerts/metrics aggregation pending)
- [ ] Cost tracking (actual vs estimated)
- [ ] Temp source cleanup tied to generation_run (currently age-based only)
- [x] LangSmith tracing baseline (workers + pipeline root/nodes + LLM + ingestion/research tools)

---

## Known Issues (from code review)

See `REVIEW_FINDINGS.md` and `review_findings_rerun.md` for detailed findings. Fixes applied:

**Round 1:**
- [x] **P0**: `is_safe_url` import mismatch in tools.py → fixed to use `_check_ssrf`
- [x] **P0**: `EpisodeManifest.duration_fill_layers` type mismatch (UUID vs String) → fixed to `ARRAY(String)`
- [x] **P0**: Presets with fake project_id FK → fixed to nullable project_id for presets
- [x] **P1**: WebSocket without ownership check → added JWT + ownership verification
- [x] **P1**: `GET /api/projects/{id}` missing sources/show_bible → added `ProjectDetailResponse`
- [x] **P1**: Source missing `generation_run_id` for temp lifecycle → added field
- [x] **P1**: Graph quality loops stubbed → implemented conditional logic

**Round 2:**
- [x] **P1**: Quality loop flags not set by nodes → verifier sets `verify_needs_research`, writer_revolt sets `revolt_triggered`
- [x] **P1**: Deep ingestion not triggered from pipeline → librarian_fast now calls `trigger_deep_ingestion` for temp sources
- [x] **P1**: `create_web_evidence` called without `generation_run_id` → now passed from state
- [x] **P1**: Canonicalizer and scout not called during ingestion → added to `run_ingestion` pipeline
- [x] **P2**: Run-scoped ShowBible not linked to generation run → added `generation_run_id` FK

**Round 3:**
- [x] **P1**: Run-scoped ShowBible breaks `scalar_one_or_none()` queries → all ShowBible lookups now filter `generation_run_id.is_(None)` to exclude run-scoped copies
- [x] **P1**: Deep ingestion governor is monotonic counter → now checks Celery `AsyncResult` state, prunes completed tasks, counts only actually running jobs

**Round 4:**
- [x] **P1**: `show_bible_id` in generation not checked for project ownership → added ownership/preset validation
- [x] **P1**: Deep ingestion duplicate task risk → `deep_ingest_url` sets PROCESSING immediately; candidate query excludes PROCESSING/COMPLETED sources

**Round 5:**
- [x] **P1**: Race condition between candidate selection and Celery enqueue → status set to PROCESSING atomically in `librarian_fast` before dispatching tasks

**Round 6:**
- [x] **P1**: Deep ingestion crash-window / stuck PROCESSING → simplified to enqueue-first-then-mark (idempotent worker guards duplicates), added stale PROCESSING cleanup (>1h) to cron

**Round 7:**
- [x] **P1**: Celery task registration instability after worker restart → fixed explicit task module loading in `celery_app` (`include=[...]` + explicit imports)
- [x] **P1**: Loop-bound asyncpg errors in generation workers (`Future/connection attached to a different loop`) → added dedicated worker DB session factory (`worker_db.py`, `NullPool`) and switched `app/pipeline/**` + generation worker sessions to it
- [x] **P1**: Sources could remain `pending` forever on worker DB errors → `ingest_source` now retries transient SQLAlchemy DB failures and marks source `FAILED` after retry exhaustion
- [x] **P2**: Silent ShowBible fallback masked LLM/JSON failures → removed fallback defaults, added strict payload validation + explicit `ShowBibleGenerationError`, API returns `502` on `/show-bible/generate`
- [x] **P2**: Completed sources retained stale historical errors in `metadata.error` → ingestion now clears `metadata.error` on successful completion

**Rounds 8-13 (runtime fixes):**
- [x] **P1**: Deep ingestion ENQUEUED status to prevent duplicate Celery dispatch
- [x] **P1**: `status_changed_at` field on Source for accurate stale detection (was using `created_at`)
- [x] **P2**: Cleanup cron hourly (was daily, mismatched 1h stale cutoff)
- [x] **P1**: Library/model updates — `claude-sonnet-4-6`, `eleven_v3`, SDK version bumps
- [x] **P0**: `audioop-lts` backport for Python 3.13+ (pydub dependency)
- [x] **P0**: Robust LLM JSON parsing — `parse_llm_json()` utility handles markdown fences, truncated responses, empty output (was bare `json.loads(raw)` in 12 pipeline files)
- [x] **P0**: `max_tokens` default raised to 16384 for structured LLM calls (decomposer/showrunner output was truncated at 4096)
- [x] **P0**: Qdrant client `search()` → `query_points()` (API removed in qdrant-client 1.17+)
- [x] **P1**: Next.js rewrites don't forward Authorization header on POST → frontend uses direct API calls (`NEXT_PUBLIC_API_URL=http://localhost:8000`)

**Round 14 (resume hardening):**
- [x] **P0**: Resume now uses LangGraph Postgres checkpointer (`AsyncPostgresSaver`) with persistent `langgraph_thread_id`; retry continues from checkpointed state
- [x] **P1**: Retry idempotency for run-unique artifacts (`EpisodeManifest`, `Episode`) to prevent unique-constraint crashes on repeated attempts

**Round 15 (episode artifact integrity):**
- [x] **P0**: Prevent false `COMPLETED` runs with empty episode artifacts — pipeline now validates `audio_file_path`, non-empty transcript text, and positive duration before returning `episode_id`
- [x] **P1**: Shownotes phase is now strict about phase ordering — it fails if `episode_id` is missing/invalid or if audio/transcript artifacts are absent
- [x] **P1**: TTS phase now fails fast when transcript timeline is empty/zero-length (instead of allowing downstream empty episodes)

**Round 16 (planning reliability hardening):**
- [x] **P1**: `showrunner` now normalizes LLM output (`dict` or top-level `list`) and validates non-empty acts before writing manifest
- [x] **P1**: `act_planner` now accepts both output shapes (`{"beat_sheet": [...]}` or `[...]`), normalizes scenes, retries on invalid output, and fails explicitly with root-cause error instead of silently writing empty beat sheets
- [x] **P1**: `integrator` now validates every act has a non-empty beat sheet, so empty scene plans fail in phase 5 (not later in screenwriter)
- [x] **P1**: Recovery path for resumed/legacy runs with empty beat sheets — `integrator` re-runs `act_planner` once before failing
- [x] **P0**: Fixed root cause of “missing beat_sheet after plan_acts” — JSONB nested in-place mutation on `EpisodeManifest.acts_json` was not tracked; phase5 now uses `deepcopy` + re-assignment to force persistence

**Round 17 (screenwriter template bugfix):**
- [x] **P0**: Fixed `KeyError` in `screenwriter` prompt template (`str.format` collided with JSON braces in system prompt); switched to explicit token replacement for `max_reply_tokens`
- [x] **P0**: Removed silent scene skipping in `screenwriter`; per-scene generation now retries and then fails the run explicitly (no partial scripts with missing scenes)

**Round 18 (scene-level resume in phase6):**
- [x] **P0**: `screenwriter` now persists progress per scene (`flush + commit` per successful scene), so retries can continue from remaining scenes instead of recomputing all completed scenes
- [x] **P0**: `screenwriter` now reuses previously completed scene segments for the same run (checkpoint-aware), but still rewrites scenes explicitly marked for revision
- [x] **P1**: Added DB-level uniqueness for script scenes per run (`generation_run_id + act_number + scene_number`) with migration and duplicate cleanup

**Round 19 (review r6 — duplicate segment hardening):**
- [x] **P1**: `act_planner` normalization now guarantees unique `scene` numbers within each act
- [x] **P1**: `screenwriter` now skips duplicate scene numbers in a beat sheet and emits deduplicated `script_segment_ids`
- [x] **P1**: Downstream dedupe guards for `script_segment_ids` added in `writer_revolt`, `citation_auditor`, and `tts` to avoid repeated processing/audio duplication from stale states

**Round 20 (review r7 — revision semantics + migration safety):**
- [x] **P1**: `screenwriter` rewrite predicate now uses explicit revision marker (`audit_status = REVISED`) instead of monotonic `revision_count`, so previously revised scenes are reused unless they are marked again
- [x] **P1**: Script-segment uniqueness migration now keeps the newest duplicate row (`updated_at DESC NULLS LAST, created_at DESC, id DESC`) to preserve latest corrected scene content

**Round 21 (observability baseline):**
- [x] **P1**: Added LangSmith tracing layer (`app/observability`) with safe no-op fallback and protected trace updates
- [x] **P1**: Instrumented Celery roots (`worker.run_generation_pipeline`, `worker.ingest_source`, `worker.deep_ingest_url`, cleanup task)
- [x] **P1**: Instrumented LangGraph execution (`pipeline.execute`) and all graph nodes (`pipeline.node.*`)
- [x] **P1**: Instrumented LLM/embedding calls and wrapped Anthropic/OpenAI clients for provider-level traces
- [x] **P1**: Instrumented ingestion/research tools with compact I/O metadata for debuggable end-to-end traces

**Round 22 (pipeline quality — doc/code alignment D3/D5/D9/D10):**
- [x] **P1**: Deepgram `language="en"` hardcoded → auto-detect (D9): `extract_video` now accepts optional `language` param; `run_ingestion` loads `Project.language` and passes it; `None` triggers nova-3 auto-detection
- [x] **P1**: Verifier was pure rule-based (D5): hybrid rule-based + LLM — pre-filter handles obvious cases (no evidence, counter evidence, opinion/report), Sonnet evaluates semantic alignment for FACT claims with evidence, batched by 8
- [x] **P1**: Deep ingestion didn't upgrade claims (D3): after `deep_ingest_url` completes, `_upgrade_claims_for_source` re-verifies all claims linked to the source's evidence via `verify_single_claim`
- [x] **P1**: Act planner had no evidence gap fill (D10): `_fill_evidence_gaps` detects narrative scenes without evidence, searches existing corpus via `vector_search_tool`, extracts quotes with Haiku, creates `EvidenceSpan` records (max 3 searches per act)

Deferred (by design):
- **P2**: OAuth endpoints (using dev-login for development)
- **P2**: Missing frontend pages (episodes list, register) — low priority
- **P2**: Full web cascade (FireCrawl + Playwright) — Jina + httpx sufficient for MVP
- **P2**: tus uploads, wavesurfer.js — nice-to-have UX features
