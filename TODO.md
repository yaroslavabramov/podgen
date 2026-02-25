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
- [x] Step-by-step generation controls in UI (run next step / rerun last step)
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
- [ ] Step-mode API/worker integration tests (`/steps/next`, `/steps/rerun`, `/steps/stop`, checkpoint rewind, stop ack path)
- [ ] Step-mode concurrency hardening (single in-flight action guarantee under concurrent requests)
- [ ] Structured-output hardening: migrate rigid JSON nodes from freeform JSON + `parse_llm_json` to Anthropic tool_use/json-schema
- [x] Web ingestion quality gates (A/B/C): anti-bot detection, web cleaner, metadata eligibility gate before canonicalizer date extraction
- [x] Canonicalizer date extraction hardening: replace any bare `json.loads(raw)` with tolerant parser helper + persist extraction status/reason in source metadata
- [x] WEB title fallback extraction when `Title:` is missing (H1/HTML `<title>`/domain fallback)
- [x] Ingestion quality test pack: captcha/403 fixture, noisy whitespace/navigation fixture, markdown-heavy fixture; verify gate decisions and skip behavior
- [x] Wikipedia ingestion reliability: MediaWiki API fast-path for `wikipedia.org/wiki/*` + shared fetch default headers (`User-Agent`, `Accept-Language`) to reduce 403 fallback to noisy Jina output
- [ ] LangSmith trace assertions for ingestion quality gates (`ingestion.web_quality.gate_a|gate_b|gate_c`)
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

**Round 23 (step-by-step execution mode):**
- [x] **P1**: Added generation `execution_mode` (`full|step`) and step control state in run config
- [x] **P1**: Added step execution endpoints: `POST /api/generations/{id}/steps/next` and `POST /api/generations/{id}/steps/rerun`
- [x] **P1**: Added worker task `run_generation_step` that executes exactly one LangGraph node using checkpoints + interrupt
- [x] **P1**: Added `generation_paused` realtime event and UI step controls for node-by-node debugging

**Round 24 (step controls hardening + stop):**
- [x] **P1**: Fixed step-mode failed retry action in UI: `retryFailedRun` now retries failed `next` step (not rerun previous step)
- [x] **P1**: Added API precondition for `/steps/rerun` to prevent invalid rerun without completed checkpointed step
- [x] **P1**: Added `POST /api/generations/{id}/steps/stop` with stop-request flagging in step control
- [x] **P1**: Added cooperative stop handling in worker (`StepExecutionStopped` -> `queued` + `generation_paused(stopped=true)`, no forced `FAILED`)
- [x] **P1**: Added stop checks before costly calls (LLM/embeddings, web tools, TTS lines) to reduce unnecessary spend after stop request

**Round 25 (web extraction — trafilatura):**
- [x] **P1**: Replaced regex-based boilerplate cleanup with trafilatura as primary web extractor: trafilatura (local, free) → Jina (JS fallback) → httpx+HTMLParser (last resort)
- [x] **P1**: Simplified Gate B (`clean_web_text`): removed site-specific regex hacks (HKMA checklist, search UI, lang toggle, font-size buttons), kept only markdown link cleanup + whitespace normalization for Jina fallback path
- [x] **P2**: Reverted over-broad `_INLINE_LINK_RE` change that stripped all markdown links (including relative/anchor links)

**Round 26 (web extraction — security + metadata):**
- [x] **P1**: SSRF bypass via redirect — `_safe_fetch()` validates every redirect hop with `_check_ssrf()`, replaces `follow_redirects=True` across all extractors
- [x] **P1**: Auth header leak on cross-origin redirect — `_safe_fetch()` strips `Authorization` header when redirect target origin differs
- [x] **P1**: Trafilatura `favor_recall=True` pulled nav garbage — removed, default precision/recall balance produces clean output
- [x] **P1**: Removed fragile heuristics (`_looks_like_article`, `_strip_leading_nav`) that caused false positive content loss
- [x] **P2**: Trafilatura metadata date extraction — `extract_web()` returns `(text, title, date)`, router writes `published_at` before canonicalizer, skipping LLM date extraction
- [x] **P2**: Updated all callers/tests for new `extract_web()` 3-value return signature

**Round 27 (scout content-map structure upgrade):**
- [x] **P1**: Replaced flat chunk summaries with source-level scout summaries (`source`, `url`, `published_at`, `total_tokens`, `chunk_count`, `section_count`, `structure_depth`, `sections[]`)
- [x] **P1**: Added lead extraction per section (first informative sentence) instead of `chunk.content[:200]`
- [x] **P1**: Added cross-source heading clusters for duplicate-theme compression
- [x] **P1**: Updated scout Haiku prompt/input payload to source-level format and added unit tests for summary aggregation/lead/clusters

**Round 28 (web extraction — Wikipedia reliability):**
- [x] **P1**: Added MediaWiki API extractor for `wikipedia.org/wiki/*` before trafilatura/Jina/httpx cascade to avoid nav-heavy fallback content
- [x] **P1**: Added explicit default fetch headers in shared `_safe_fetch` (`User-Agent`, `Accept-Language`) to reduce avoidable 403 responses on article pages
- [x] **P2**: Added tests for Wikipedia URL parsing, section-heading conversion, and cascade behavior when MediaWiki succeeds/falls back

**Round 35 (web extraction — review follow-ups):**
- [x] **P2**: Tightened Wikipedia host check to exact domain boundary (`wikipedia.org` or `*.wikipedia.org`) to avoid false positives like `notwikipedia.org`
- [x] **P2**: Fixed namespace parsing: decode slug before namespace check, reject only known namespace prefixes, allow valid article slugs with `:` (e.g. `Star_Trek:_...`)
- [x] **P2**: Added regression tests for non-Wikipedia host, encoded namespace (`Category%3A...`), and valid colon article slug
- [x] **P2**: Wikipedia fast-path now returns date from first revision timestamp (`rvdir=newer&rvlimit=1`) so `published_at` is filled and LLM date extraction is skipped

**Round 36 (chunking reliability — oversized web blocks):**
- [x] **P1**: Fixed oversized chunk leak to embeddings: `_split_large_chunk` now hard-splits huge single blocks instead of returning >8k-token chunks
- [x] **P1**: Added split fallback chain for flat web text: paragraph (`\n\n`) → single newline lines → sentence packing → hard token windows
- [x] **P2**: Added regression tests for oversized single paragraph and single-newline flat text

**Round 37 (debug tooling — extractor raw dumps):**
- [x] **P2**: Added optional file dump for raw extractor outputs (`mediawiki/trafilatura/jina/httpx`) with status/title/date/error metadata
- [x] **P2**: Added env flag `WEB_EXTRACTOR_DEBUG_DUMP_DIR` (disabled by default) to enable/disable dumps without code changes
- [x] **P2**: Added tests for debug dump write path and disabled path

**Round 38 (scout quality — unstructured source coverage):**
- [x] **P1**: Root-only chunk sources no longer collapse into a single scout section; each root chunk is kept as its own section for better content-map coverage
- [x] **P2**: Added unit test for unstructured multi-chunk source summary behavior (`section_count == chunk_count`)

**Round 39 (chunking quality — universal heading inference):**
- [x] **P1**: Added deterministic heading inference for non-markdown web text (outline prefixes, section labels, title-like short lines) before fallback flat chunking
- [x] **P1**: Added confidence guards to avoid false structure from TOC/navigation lists (requires supporting body + heading density cap)
- [x] **P2**: Added regression tests for inferred outline structure and TOC-like false-positive rejection

**Round 40 (chunking reliability — pack boundary fix):**
- [x] **P1**: Fixed `_pack_segments` token budgeting bug by validating real joined chunk token count (`_count_tokens(candidate)`) instead of summing isolated segment counts
- [x] **P2**: Added deterministic regression test for join-boundary overflow case (`max_packed_tokens` must be `<= TARGET_MAX_TOKENS`)

**Round 41 (test hygiene — tracing isolation):**
- [x] **P2**: Disabled LangSmith tracing in pytest by default (`tests/conftest.py`: env + runtime settings override) to prevent test runs from polluting operational traces
- [x] **P2**: Added explicit rule in `DEVELOPMENT_RULES.md` that test runs must use `LANGSMITH_TRACING=false`

**Round 42 (summary quality — merge/lead/section-cap fixes):**
- [x] **P1**: Chunk merge now requires identical `heading_path` (no cross-section merge contamination)
- [x] **P1**: Scout lead extraction now prefers complete sentence starts and skips overlap-like lowercase fragments
- [x] **P1**: Replaced fixed per-source section cap with adaptive cap based on source size and number of sources in prompt
- [x] **P2**: Added regression tests for heading-preserving merge behavior, lead fragment filtering, and adaptive section cap behavior

**Round 43 (ingestion reliability — transient DNS failures):**
- [x] **P1**: Added bounded DNS retry in SSRF pre-check (`WEB_DNS_RESOLVE_RETRIES`, `WEB_DNS_RESOLVE_RETRY_DELAY_MS`) with explicit `DNSResolutionError`
- [x] **P1**: Added Celery retry for `ingest_source` on DNS resolution failures instead of immediate hard failure
- [x] **P2**: Added unit tests for DNS resolve retry success/failure paths and documented behavior in development rules

**Round 44 (chunking quality — nested heading structure):**
- [x] **P0**: Fixed `_has_supporting_body()` discarding parent headings when child headings follow without intermediate text — child headings (level > current) are now transparent, only siblings/parents stop lookahead
- [x] **P0**: Fixed `_split_by_inferred_headings()` not updating `current_h2` for bodyless parent headings — heading path now propagates correctly to child sections
- [x] **P1**: Removed `article` from `_SECTION_LABEL_RE` — articles are document content, not structural headings
- [x] **P1**: Added explicit guard against `Article N ...` lines passing the generic capitalized-words heuristic in `_infer_heading_level()`
- [x] **P2**: Added regression tests for nested chapter heading_path, child-heading transparency, TOC rejection, and article exclusion

**Round 45 (scout quality — de-overlap leads):**
- [x] **P1**: Scout now strips chunk overlap prefix before lead extraction, using previous base chunk tail (`OVERLAP_TOKENS`) so section leads start from section-owned content
- [x] **P2**: Added regression tests proving `Section 3` lead does not inherit `Article 65` carry-over from previous section, plus mismatch-safe no-op behavior

**Round 46 (web extraction quality — automatic PDF promotion):**
- [x] **P1**: Added direct PDF URL fast-path in web extractor (`.pdf` / `downloadpdf`) with extraction via `extract_pdf_bytes` (no manual user PDF upload required)
- [x] **P1**: Added nav-heavy Jina detection + same-host `Download PDF` promotion path to auto-switch from noisy portal markdown to article PDF
- [x] **P2**: Added Jina published-date extraction (`Published Time: YYYY-MM-DD`) and carried date through promoted-PDF path
- [x] **P2**: Refactored `app/ingestion/pdf.py` to expose reusable `extract_pdf_bytes(...)` for web ingestion and added regression tests for new web cascade branches

**Round 47 (chunking quality — markdown AST headings):**
- [x] **P1**: Replaced regex-only markdown split (`##/###`) with `markdown-it-py` token parsing for headings H1-H6 (ATX + Setext)
- [x] **P1**: Preserved heading hierarchy generically by markdown level (not hardcoded to H2/H3), while keeping plain-text heading inference fallback when markdown headings are absent
- [x] **P2**: Added regression tests for setext headings and deep-level markdown headings (`#` + `####`) to ensure heading_path is built correctly

**Round 48 (chunking quality — multilingual markdown headings):**
- [x] **P1**: Fixed markdown heading validation to support Cyrillic headings (previously non-Latin headings were rejected, causing fallback to flat chunking)
- [x] **P2**: Added regression test for Russian markdown heading hierarchy (`# Введение` → `## Детали`)

**Round 49 (scout quality — navigation noise suppression):**
- [x] **P1**: Added section-level noise filtering for web source summaries to remove portal/navigation/UI sections before content-map LLM call
- [x] **P1**: Added source-level web quality weighting (downweight nav-heavy sources; exclude navigation-only sources) to prevent content-map domination by noisy portals
- [x] **P2**: Added regression tests for section filtering, navigation-only source exclusion, and nav-heavy source downweight ordering

**Round 50 (scout quality — metrics/keywords false-positive fix):**
- [x] **P1**: Refined navigation-noise heuristic scoring: `metrics/keywords` moved to weak signals so standalone substantive sections are not dropped
- [x] **P2**: Added regression tests ensuring legitimate `Metrics` / `Keywords` content sections are preserved

**Round 51 (scout quality — language-agnostic noise scoring):**
- [x] **P1**: Replaced dictionary-centric web section filter with language-agnostic structural scoring (heading depth/prefix dominance, list/link density, prose-vs-index signals)
- [x] **P1**: Added source-level exclusion for nav-heavy low-prose web sources based on aggregate noise/prose ratios (without language-specific keyword dictionaries)
- [x] **P2**: Updated engineering rules/docs to enforce language-agnostic scout filtering policy

**Round 52 (scout quality — non-whitespace script handling):**
- [x] **P1**: Improved section noise scoring/tokenization for non-whitespace scripts (CJK/Hiragana/Katakana/Hangul) and full-width separators so taxonomy-style portal sections are detected consistently
- [x] **P2**: Improved lead extraction for no-space punctuation scripts: sentence split now works without whitespace after punctuation, with CJK-aware candidate scoring
- [x] **P2**: Added regression tests for CJK navigation noise detection, CJK source downweighting, and CJK lead sentence selection

**Round 53 (PDF ingestion reliability — marker timeout fallback):**
- [x] **P1**: Added hard timeout for marker-pdf conversion (`PDF_MARKER_TIMEOUT_SECONDS`) to prevent long worker stalls on first model download/init
- [x] **P1**: Added automatic fallback chain on marker timeout/error: `pypdfium2` → `PyPDF2`
- [x] **P2**: Added regression tests for marker success, exception fallback, and timeout fallback behavior

**Round 54 (PDF ingestion reliability — marker prewarm compatibility):**
- [x] **P1**: Added `surya`/`transformers` compatibility shim in PDF marker path to prevent `KeyError('encoder')` during marker model initialization
- [x] **P1**: Added compatibility patch for composite Surya configs (`get_text_config`) to avoid transformers ambiguity errors when both `text_encoder` and `decoder` are present
- [x] **P1**: Added explicit `prewarm_marker_models()` helper so model predownload runs through app compatibility layer
- [x] **P2**: Added regression test validating empty `SuryaOCRConfig()` init works after compat patch
- [x] **P2**: Added regression test validating table-rec composite config text-config resolution after compat patch
- [x] **P2**: Updated README/DEVELOPMENT_RULES with the supported prewarm command and policy

**Round 55 (PDF ingestion reliability — surya S3 retry path bug):**
- [x] **P1**: Patched `surya.common.s3.S3DownloaderMixin.from_pretrained` retry logic to preserve remote `s3://...` path across retries (prevents broken `https://models.datalab.to/<absolute-local-path>/manifest.json` 404 sequence after first transient failure)
- [x] **P2**: Added regression test that reproduces first-attempt failure and verifies second attempt still uses remote model path

**Round 56 (PDF ingestion reliability — true hard timeout in Celery):**
- [x] **P1**: Replaced marker execution path from default loop executor to dedicated subprocess with hard timeout + terminate/kill, preventing `asyncio.run()` default-executor shutdown stall in Celery tasks
- [x] **P2**: Updated timeout/fallback unit tests for new execution path and added regression test validating stuck subprocess is terminated on timeout
- [x] **P2**: Updated PIPELINE/IMPLEMENTATION/DEVELOPMENT_RULES to document subprocess hard-timeout contract

Deferred (by design):
- **P2**: OAuth endpoints (using dev-login for development)
- **P2**: Missing frontend pages (episodes list, register) — low priority
- **P2**: Full web cascade (FireCrawl + Playwright) — trafilatura + Jina + httpx sufficient for MVP
- **P2**: tus uploads, wavesurfer.js — nice-to-have UX features
