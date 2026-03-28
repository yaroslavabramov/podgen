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

- [x] Claims/Evidence lineage refactor: add `Source.authority_type`, `EvidenceProvenance`, and upgraded `ClaimEvidence` flags (`supports_fact`, `contradicts_fact`, `illustrative`)
- [x] Claims/Evidence lineage refactor: canonical source identity + exact evidence dedupe path for hunter/librarian/planning-created evidence
- [x] Claims/Evidence lineage refactor: branch-based claim batching with source+lineage payload enrichment
- [x] Claims/Evidence lineage refactor: global claim dedupe before verifier
- [x] Claims/Evidence lineage refactor: verifier hardening (`is_duplicate_of`, `is_temporary`, attribution quality, fresh `research_gaps`)
- [x] Claims/Evidence lineage refactor: language-aware keyword retrieval and broad+targeted librarian web search
- [x] Claims/Evidence lineage refactor: tests and docs
- [ ] End-to-end tests
- [ ] Step-mode API/worker integration tests (`/steps/next`, `/steps/rerun`, `/steps/stop`, checkpoint rewind, stop ack path)
- [ ] Step-mode concurrency hardening (single in-flight action guarantee under concurrent requests)
- [ ] Ingestion content-map consistency under concurrent source ingests (ensure project map build sees full committed corpus, not a partial subset when multiple ingests finish close together)
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
- [x] Screenwriter prompt payload reduction: remove redundant act-level abstractions, add `previous_scene` + `already_covered_claims` local memory, and bias scene writing toward callbacks over re-explanations
- [x] Screenwriter scene-level debug dumps: persist prompt payload, attempt history, raw/parsed LLM output, and final dialogue for each scene
- [x] Screenwriter continuity upgrade: add screenplay-level covered-claim memory plus `previous_act_summary` for first scenes so later acts build on prior explanations instead of resetting
- [x] ~~Screenwriter delta framing: add `new_information_goal` prompt field~~ — superseded: `new_information_goal` removed and replaced by `prior_scenes_dialogue` + `callback_claims` objects
- [x] Screenwriter dialogue de-rigidification: loosen A/B role contract, discourage strict `ABAB` alternation and repeated opener phrases, and add light evidence-attribution guidance for dates/rules/figures
- [x] Screenwriter local anti-echo + discourse-shape variation: scenes should advance rather than paraphrase the previous punchline, vary agreement/challenge shapes without word blacklists, and normalize speaker labels to canonical `A/B` including ShowBible host-name / role aliases
- [x] Writer Revolt style rubric expansion: evaluator can now flag rigid turn-taking, repetitive agreement/challenge shapes, scene-opening echo, and teacherly exposition as rewrite reasons

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

**Round 25 (phase 6 quality stack, first actionable slice):**
- [x] **P1**: Added `script_segments.audit_findings` JSONB + migration so phase 6 audits persist structured findings, not only scalar verdicts
- [x] **P1**: `writer_revolt` replaced the old pure `score < 0.6` gate with boundary-aware dialogue audit findings (`severity` / `scope`) and inline `scene_polish` for repairable flow/style issues
- [x] **P1**: Propagated explicit `language` through Phase 6 dialogue audit/polish prompts, so engagement checks and polish preserve spoken register in the dialogue language instead of relying on implicit inference
- [x] **P1**: Reworked citation question post-filter toward structured multilingual audit signals (`line_function`, `action_required`) instead of system-level phrase heuristics
- [x] **P1**: Structured citation audit signals now affect non-question transitions/synthesis and actual action semantics (`passed`, repair trigger, blocking), not just helper-level question downgrades
- [x] **P1**: Full scene regeneration remains only as fallback via `audit_status = REVISED` for `scene_rewrite` findings; polished scenes are reused and stale audit findings are cleared on screenwriter rewrite
- [x] **P1**: `citation_auditor` now persists structured citation issues, attempts scene-level citation repair, re-audits repaired scenes, and blocks audio on unresolved high-severity rigor failures
- [x] **P1**: Added phase 6 debug dumps for `dialogue_audit`, `dialogue_polish`, `citation_audit`, and `citation_repair`
- [x] **P1**: Added prompt/helper regression tests for new dialogue audit/polish and citation repair/blocking semantics

**Round 26 (phase 6 reviewer follow-up):**
- [x] **P1**: Extracted shared phase 6 dialogue helpers into `dialogue_utils.py` so audit/polish modules no longer import private `screenwriter` internals
- [x] **P1**: Switched citation detection to Haiku; Sonnet remains only for `citation_repair`
- [x] **P1**: Tightened citation blocking semantics so unresolved `scope=block` findings block audio, while high-severity `record` findings do not
- [x] **P1**: Added `scene_job` to dialogue audit/polish payloads so scene-level polish preserves the intended move
- [x] **P1**: Expanded regression tests for scope-based citation blocking, Haiku citation detection, and dialogue polish payload context
- [x] **P1**: Narrowed citation audit context to scene-relevant claims (`dialogue.claim_ids` + manifest scene claims) instead of sending the whole episode claim universe into every scene audit
- [x] **P1**: Relaxed citation audit expectations for non-assertive lines (questions / callbacks / synthesis) so `missing_claim_link` focuses on new factual assertions
- [x] **P1**: Reworked citation repair acceptance around blocker resolution + claim preservation + bounded line drift, instead of noisy full-issue-count comparison
- [x] **P1**: Added bounded citation-audit retry for transient failures and a second citation-repair attempt for blocking scenes, with compact retry feedback instead of immediate audio block on the first bad repair

**Round 27 (phase 6 citation fail-fast + line repair):**
- [x] **P1**: Added a non-assertive post-filter so question lines no longer become false citation blockers by default
- [x] **P1**: Split citation repair into `line_repair` first and `scene_repair` fallback, with mechanical splice preserving all unflagged lines verbatim
- [x] **P1**: Replaced scene-wide claim preservation with assertive-line-aware coverage checks, so repaired question lines may drop stale `claim_ids` without false rejection
- [x] **P1**: Switched `audit_citations` to fail-fast mode so the node stops spending after the first unresolved blocking scene
- [x] **P1**: Bulk-loaded claims in `citation_auditor` instead of one SQL round-trip per claim
- [x] **P1**: Tightened the non-assertive guard after review: only question lines get deterministic claim-link downgrades; short factual recap lines keep full grounding requirements
- [x] **P1**: Reworked citation repair prompts around spoken podcast voice: same-language register, preserved agency, local clause repair before deletion, scene-position awareness, and contrastive anti-kanzelarit / anti-double-hedging examples
- [x] **P1**: `scene_repair` now receives flagged-line payloads and is told to focus edits on flagged lines while leaving clean lines as intact as possible; only a tiny whitelist of genuinely generic questions downgrades `unverified_stated_as_fact` to `record`, while recap / confirmation questions and loaded wh-questions remain actionable

**Round 28 (phase 6 writer/editor completion slice):**
- [x] **P1**: `writer_revolt` now audits all scenes first, builds cheap act-level repetition/distribution reports, and prioritizes polish per act instead of spending a de facto episode-global budget on early scenes
- [x] **P1**: `dialogue_polish` acceptance is now issue-aware for opener echo/restatement; polish that keeps the same opener move is rejected
- [x] **P1**: `screenwriter` prompt gained stronger anti-echo contrastive opener guidance, so continuation means consequence/pressure/question rather than soft paraphrase of the prior landing
- [x] **P1**: Added `writer_original_dialogue_json` + migration so downstream repairs no longer erase the last screenwriter baseline for a scene
- [x] **P1**: `scene_repair` metadata merge now preserves `emotion` / `ssml_hints` where repaired lines stay textually aligned with the original
- [x] **P1**: Split writer-side intervention accounting from graph rewrite-loop accounting (`writer_revolt_counts_by_act` vs `writer_planner_loops`) and added a dedicated graph rewrite-loop setting

**Round 29 (phase 6 full-plan closeout):**
- [x] **P1**: Added compact act-level `dialogue_audit` on scene-edge summaries, so repeated opener / confirmation / interruption families are prioritized as act-level distribution problems
- [x] **P1**: `citation_repair` is now line-role-aware; sensitive opener / landing / punchline lines prefer `line_repair`, and unflagged sensitive lines are protected from scene-wide rewrites
- [x] **P1**: Non-blocking citation repair acceptance now evaluates changed/relevant lines instead of relying on noisy full-scene re-audit stability
- [x] **P1**: `available_claims` now carry attribution/source-support metadata and the citation taxonomy now includes `unsupported_source_framing`

**Round 30 (phase 6 real act audit + seam pass):**
- [x] **P0**: Replaced edge-only act audit surrogate with real full-act Haiku audit over full scene dialogue, `scene_job`, local issue types, and structured opener/turn-pattern context
- [x] **P0**: Added compact episode seam audit across act boundaries so `replay_question` / `replay_punchline` / `weak_reset` can target the opening scene of the next act
- [x] **P0**: Reworked `writer_revolt` prioritization so act/seam findings can trigger `dialogue_polish` even when no local medium issue exists, instead of limiting polish to scene-local findings only
- [x] **P0**: Tightened `dialogue_polish` acceptance around concrete targets: opener-targeted and seam-targeted interventions now require a material opener change, and speaker-distribution interventions reject polish that leaves the opener in the same dominant-speaker skew
- [x] **P0**: Made `dialogue_polish` metadata-safe by contract: polish prompt now returns `emotion` / `ssml_hints`, same-length rewrites get safe metadata backfill, and changed-length rewrites are accepted only when returned delivery metadata remains usable
- [x] **P1**: Strengthened citation repair guidance for mechanism-heavy explanation lines and unsupported quote/source framing without adding language-specific phrase lists
- [x] **P1**: Expanded writer/citation regression coverage for full-act payloads, seam audit normalization, act-targeted polish, and changed-length metadata-preserving polish
- [x] **P1**: Screenwriter, dialogue polish, and citation repair now keep spoken lines in the podcast language by contract; foreign-language source text must be translated/paraphrased unless the wording itself is explicitly the point of the beat
- [x] **P1**: Screenwriter, dialogue polish, and citation repair now enforce canonical `A/B` speaker IDs by prompt and runtime normalization; ShowBible host names / roles are mapped back to `A/B` instead of silently collapsing unknown labels to `A`

**Round 69 (fast-web retrieval + quote truncation hardening):**
- [x] **P0**: Remove fast-web `[:3]` gap cap and switch governor semantics to processed gaps instead of evidence items

**Round 70 (phase 6 speaker-label normalization hardening):**
- [x] **P1**: Fixed generic `Host B:` / `Speaker B:` punctuation path in phase-6 speaker normalization so writer / dialogue polish / citation repair cannot silently flip B-lines to `A`
- [x] **P0**: Add language-aware fast-web query generation with explicit English rewrite for non-English questions
- [x] **P0**: Stop truncating verifier supporting quotes to 300 chars; verifier must see the real quote text
- [x] **P1**: Rework claim-builder batching to budget against real quote lengths instead of pre-truncated `[:700]` payloads
- [x] **P1**: Add regression tests for fast-web gap selection/governor/query rewrite and verifier/claim-builder quote handling
- [x] **P1**: Update docs for fast-web retrieval strategy, governor semantics, and quote payload policy
- [x] **P1**: Added API precondition for `/steps/rerun` to prevent invalid rerun without completed checkpointed step
- [x] **P1**: Added `POST /api/generations/{id}/steps/stop` with stop-request flagging in step control
- [x] **P1**: Added cooperative stop handling in worker (`StepExecutionStopped` -> `queued` + `generation_paused(stopped=true)`, no forced `FAILED`)
- [x] **P1**: Added stop checks before costly calls (LLM/embeddings, web tools, TTS lines) to reduce unnecessary spend after stop request

**Round 70 (fast-web rework + incremental verifier):**
- [x] **P0**: Replace Jina-only fast-web fetch path with shared lightweight extraction stack (`mediawiki -> trafilatura -> jina`) plus `clean_web_text(...)` before extractor LLM
- [x] **P0**: Replace blind `content[:5000]` prompt payload with relevance-based excerpt windows built from cleaned page text
- [x] **P0**: Skip known canonical sources before fetch/extract when source is already fully ingested and local content/chunks are available
- [x] **P0**: Enrich verifier-generated `research_gaps` with useful search guidance (`claim_statement`, `support_strength`, `search_objective`, etc.)
- [x] **P0**: Make verifier-loop fast-web query generation corroboration-oriented, using claim-level signals instead of only question text
- [x] **P1**: Split fast-web policies into `hunter_gap_fill` vs `verifier_corroboration`
- [x] **P1**: Split fast-web governors/budgets for initial gap fill vs verifier-loop follow-up
- [x] **P1**: Add cheap page reranking before fetch using novelty, domain preference, and overlap with search objective
- [x] **P1**: Expand extractor prompt payload with verifier-loop context (`gap_origin`, `claim_statement`, `existing_support_summary`, `search_objective`)
- [x] **P1**: Strengthen fast-web query generation from `question + claim_statement + structured search objective`
- [x] **P1**: Improve fast-web observability/debug dumps to show selected gaps, query variants, skipped known URLs, excerpt windows, filter reasons, and accepted/rejected evidence
- [x] **P0**: Make verifier incremental after `dedupe_claims`, so loop iterations verify only claims whose verifier-relevant payload changed
- [x] **P0**: Have `build_claims` return explicit rebuild scope metadata (`rebuilt_claim_ids`, `preserved_claim_ids`, `targeted_branch_ids`) for downstream verifier scoping
- [x] **P0**: Have `dedupe_claims` return touched-scope metadata (`dedupe_deleted_claim_ids`, `dedupe_merged_into_claim_ids`, `dedupe_untouched_claim_ids`)
- [x] **P0**: Add `verification_fingerprint` and verifier scope selection after dedupe based on changed payload, not just new claim IDs
- [x] **P1**: Ensure all rebuild/dedupe coordination fields are ephemeral and cleared from state after verifier consumes them
- [x] **P2**: Add cheap low-value page filter after cleanup to avoid spending extractor LLM budget on boilerplate/shell pages
- [x] **P2**: Add adaptive fetch depth for verifier corroboration gaps
- [x] **P2**: Add cross-gap source-family diversification pressure for repeated explainer/Wikipedia-heavy loops
- [x] **P2**: Persist `verification_fingerprint` on `Claim` for future retry/resume optimization across runs

**Round 71 (fast-web verifier-mode retrieval redesign):**
- [x] **P0**: Replace mixed native/deterministic fast-web query building with universal LLM query synthesis for all web-search modes and languages
- [x] **P0**: Remove deterministic `search_objective + asset keywords` query expansion from final search queries
- [x] **P0**: Separate fast-web extractor payload semantics into `question_gap_fill` vs `claim_corroboration`
- [x] **P0**: For verifier-origin gaps, stop using question-level `required/expected/missing assets` as primary extraction targets
- [x] **P1**: Make verifier-mode reranking and excerpt selection claim-anchor-first instead of blended question+claim overlap
- [x] **P1**: Add regression tests for universal query synthesis, verifier extractor payload contract, and verifier-mode page/window filtering
- [x] **P1**: Update docs for fast-web verifier-mode redesign and LLM-first query policy

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

**Round 57 (web/scout quality — universal nav handling without aggressive drops):**
- [x] **P1**: Gate B cleanup now preserves setext heading underlines and removes additional UI artifacts safely (`* Skip to ...` style bullet nav stubs, `javascript:` placeholder links) without site-specific regex packs
- [x] **P1**: Scout section lead selection switched from first-chunk-only to best-candidate scoring across section chunks (prose signal vs nav-noise), reducing bad leads from menu-heavy prefixes
- [x] **P1**: Scout navigation-noise scoring improved with language-agnostic structural signals (sentence-end detection robust to domains, deep heading-path taxonomy signal, bullet/camel-case menu artifacts)
- [x] **P2**: Relaxed source exclusion policy for noisy web sources: meaningful retained prose sections are downweighted but not dropped by default
- [x] **P2**: Added regression tests for setext preservation, bullet-nav/js cleanup, deep taxonomy detection, section lead replacement, and non-destructive mixed-source retention

**Round 58 (source management — manual re-ingest from UI):**
- [x] **P1**: Added backend endpoint `POST /api/sources/{source_id}/reingest` with ownership checks and in-progress guard
- [x] **P1**: Added explicit re-ingest cleanup path for prior source chunks + Qdrant vectors (`source_id` filter) to prevent duplicate retrieval artifacts
- [x] **P2**: Added Project Sources UI action (`Re-ingest`) with loading/error states and i18n labels (EN/RU)
- [x] **P2**: Added unit test for Qdrant source-filtered vector deletion helper

**Round 59 (re-ingest reliability — queue safety + duplicate dispatch guard):**
- [x] **P1**: Fixed data-loss window on re-ingest enqueue failure: moved destructive cleanup from API handler to worker path after successful Celery enqueue
- [x] **P1**: Strengthened re-ingest in-progress guard to include `PENDING`, preventing duplicate manual dispatch while initial ingestion is still queued
- [x] **P1**: Added worker-level idempotency lock (`FOR UPDATE SKIP LOCKED`) + status no-op guard so duplicate Celery deliveries for one source do not run concurrently
- [x] **P1**: Added rollback-before-fail-status in `ingest_source` error path to avoid committing partial ingestion writes on exceptions

**Round 60 (re-ingest follow-up — FK-safe cleanup + UI/API state alignment):**
- [x] **P1**: Re-ingest cleanup now detaches `EvidenceSpan.chunk_id` for source before deleting `chunks`, preventing FK failures on projects with existing evidence
- [x] **P2**: Added regression test for re-ingest reset path ensuring evidence detach + chunk delete ordering contract
- [x] **P2**: Kept UI re-ingest button disabled for `pending` to align with backend in-progress guard and avoid avoidable `409` errors

**Round 61 (scout lead follow-up — script-agnostic lead extraction):**
- [x] **P2**: Removed Latin/Cyrillic-only lead start gating in scout and replaced it with Unicode-aware, case-aware first-character checks (keeps lowercase overlap filtering for caseful scripts only)
- [x] **P2**: Expanded lead content-unit scoring with Unicode word units + alphabetic-density fallback so non-case/non-whitespace scripts are not under-scored
- [x] **P2**: Added regression test for Arabic lead selection (informative second sentence now preferred over short opener)

**Round 62 (content-map consistency under parallel ingestion):**
- [ ] **P1**: Guarantee that project `ContentMap` is built from a complete committed corpus snapshot under concurrent source ingestion (eliminate race where latest `created_at` map can be built from a partial source set)
- [ ] **P2**: Add concurrency regression test(s) for simultaneous source ingestions validating that pipeline picks a map containing all completed sources

**Round 63 (research targeting + retrieval diversity):**
- [x] **P1**: Hunter retrieval upgraded from fixed top-5 extraction to top-40 candidate pool with diversity-aware re-ranking penalties on repeated `source_id` and `parent_chunk_id`
- [x] **P1**: Hunter keyword retrieval now supports configurable `top_k`; vector+keyword retrieval expanded to 40/40 before merge
- [x] **P1**: Hunter now emits structured `research_gaps` (`missing_assets`, `expected_assets`, `found_assets`) aggregated per branch
- [x] **P1**: Librarian Fast now consumes `research_gaps` as primary branch-selection signal and targets missing asset types in web search/extraction (priority heuristic kept as compatibility fallback)

**Round 64 (decomposer/hunter evidence contract split):**
- [x] **P1**: Decomposer output schema migrated from single `expected_evidence_type` to dual contract: `required_evidence` (`type`, `min_count`) + `expected_evidence` (soft targets)
- [x] **P1**: Decomposer sanitization now validates the new schema and keeps backward compatibility by up-converting legacy `expected_evidence_type` into `required_evidence` (`min_count=1`)
- [x] **P1**: Hunter gap accounting now computes `missing_assets` strictly from `required_evidence` with `min_count` and emits `required_assets` (`type`, `min_count`, `found_count`) in `research_gaps`
- [x] **P2**: Hunter target-type normalization now filters to allowed asset enums and deduplicates consistently across required/expected signals
- [x] **P2**: Added regression tests for new decomposer contract + hunter evidence contract helpers; updated pipeline docs (`PIPELINE.md`, `PIPELINE_IO_MAP.md`, `IMPLEMENTATION.md`)

**Round 65 (hunter/librarian retrieval correctness hardening):**
- [x] **P1**: Hunter source attribution hardened: no silent fallback to first chunk/source; unresolved quote-to-chunk mapping now skips evidence with warning
- [x] **P1**: Hunter extraction prompt simplified (removed redundant `target_evidence_types` field)
- [x] **P1**: Hunter now validates `asset_type` before DB write to avoid enum poisoning from malformed LLM outputs
- [x] **P1**: Hunter `research_gaps` moved to question-level accounting (`question_id`) to avoid masking under-covered child questions
- [x] **P1**: Hunter progress/event metrics now report newly created evidence in current run (not cumulative state length)
- [x] **P1**: Keyword retrieval switched from hardcoded English FTS to `simple` config and parameterized `source_ids` binding (no string-built IN clause)
- [x] **P2**: Vector retrieval tool optimized to batch-load chunks (removed N+1 DB selects)
- [x] **P2**: Librarian Fast no longer appends raw asset enums to web query string; missing asset targeting remains in extraction/filter stage
- [x] **P2**: Added/updated regression tests for hunter evidence contract/origin resolution and librarian query behavior

**Round 66 (LLM-first pipeline quality — immediate work):**
- [x] **P0**: Propagate existing decomposition `intent` (`history|explain|compare|debate|case_study`) into Hunter extraction payload/prompt
- [x] **P0**: Propagate existing decomposition `intent` into Librarian Fast payload/prompt and branch gap context
- [x] **P0**: Propagate existing decomposition `intent` into Claim Builder payload/prompt
- [x] **P0**: Pass `ClaimEvidence.illustrative` through planning payloads so Act Planner can distinguish factual core from narrative hooks
- [x] **P0**: Ensure `Source.authority_type` is actually populated/finalized for canonical sources during ingestion/canonicalization (not left effectively `unknown`)
- [x] **P1**: Add question-level `direct_or_contextual` signal in provenance/context layer (not `EvidenceSpan`): Hunter/Librarian generation, schema/migration, and downstream reads
- [x] **P1**: Enrich Claim Builder and Verifier prompts/payloads with research-aware context: `intent`, source metadata, provenance summary, contextual asset types, and `direct_or_contextual` when available
- [x] **P1**: Enrich planning payload beyond flat claims: carry verification + illustrative + branch/question context (and lightweight source-quality hints where useful)

**Round 67 (Verifier/Planning debug dumps for analysis):**
- [x] **P0**: Add file debug dumps for verifier batches and verifier run summary (`input`, raw LLM output, parsed output, final decisions, `research_gaps`)
- [x] **P0**: Add file debug dump for showrunner (`input`, raw output, parsed output, normalized act plan)
- [x] **P0**: Add file debug dump for act planner per act (`input`, attempt history, raw outputs, normalized beat sheet, evidence gap fill result)
- [x] **P1**: Add regression tests for verifier/showrunner/act planner debug dump write path and disabled path

**Round 68 (production readiness — research/claims/verifier quality):**
- [x] **P0**: Replace full `verify -> research_fast_web -> build_claims` rebuild with an incremental targeted-update loop that preserves unaffected claims and only rebuilds impacted branch/question groups
- [x] **P0**: Harden Claim Builder prompt/payload/examples so claims stay minimal and auditable: no unsupported dates, motives, causal links, or contextual-to-fact upgrades
- [x] **P0**: Improve non-history intent quality (`debate`, `compare`, `case_study`) in Hunter and Claim Builder so retrieval/extraction/claim shaping follows editorial intent rather than drifting into generic historical background
- [x] **P1**: Tighten verifier reasoning prompt so explanations stay grounded and evidence-bound without speculative temporal/source commentary
- [x] **P1**: Improve Claim Builder illustrative-evidence behavior so ANECDOTE/CASE/EXPERT_QUOTE default more consistently to illustrative unless they truly carry the proposition
- [ ] **P1**: Run cross-topic production-readiness evaluation passes on several intentionally different corpora and document remaining universal failure modes

Deferred after Round 68:
- [ ] **P2**: Add typed verifier gap routing (`weak_source`, `truncated_quote`, `claim_overshoot`, etc.) with separate remediation paths instead of sending all `needs_research` cases through the same fast-web loop

**Round 72 (fast-web verifier-mode correctness after live run):**
- [x] **P0**: In `claim_corroboration` mode, stop rejecting extractor output via question-level `target_missing_assets`; asset-type targeting should remain only for `question_gap_fill`
- [x] **P0**: Stop swallowing `StepExecutionStopped` inside fast-web query loop; step-mode stop must abort the node instead of degrading into `search_failures`
- [x] **P0**: Remove top-level verbose verifier `search_objective` duplication from fast-web extractor payload; replace with compact corroboration-goal signal
- [x] **P1**: Fix fast-web success accounting to distinguish `accepted_extractor_items`, `new_canonical_evidence_count`, and `new_provenance_links_count`
- [x] **P1**: Revisit verifier-mode page retention for broad history/legal context pages that survive rerank/window selection without direct claim corroboration

**Round 73 (systemic follow-ups from run 11a4826f):**
- [x] **P0**: Add Hunter retry paths for extraction failures, distinguishing `exception/extraction_failed` from `invalid_extractor_payload` and retrying with narrower payload/prompt strategy
- [x] **P0**: Make fast-web page retention trust-aware end-to-end: keep authority-sensitive bypass/threshold logic in `_is_low_value_page`, log explicit drop reasons, and penalize weak source families more consistently
- [x] **P0**: Deduplicate overlapping verifier `research_gaps` by union of all intersecting `claim_ids`, merging `claim_contexts`, coverage signals, and asset summaries instead of spending multiple gap slots on the same corroboration target
- [x] **P0**: Replace fixed verifier fast-web budget with adaptive, upper-bounded, intent-aware budgeting/prioritization based on remaining gap count, with extra weight for `debate`/`compare` gaps
- [x] **P1**: Strengthen `claim_corroboration` source-quality policy so primary/secondary candidates are preferred over tertiary/unknown pages in both reranking and extractor selection
- [x] **P1**: Add explicit `expected_evidence` tracking in Hunter extraction/summary/debug output so soft editorial targets become measurable coverage signals instead of prompt-only hints
- [x] **P1**: Add bibliography/reference-window filtering in fast-web excerpt selection using citation-like heuristics/regex signals before extractor LLM spends budget on them
- [ ] **P1**: Reintroduce claim-vs-claim conflict detection as an upstream LLM adjudication pass instead of showrunner-side numeric heuristics
- [x] **P1**: Fix semantic claim-to-branch/question assignment so verifier-loop corroborates the right research target instead of following only broad provenance lineage
- [x] **P1**: Rework targeted rebuild / verifier reuse so fingerprint-based skipping can actually save LLM calls for unchanged rebuilt claims (prefer update-over-delete semantics where feasible)
- [x] ~~**P1**: Add stronger showrunner claim-density caps~~ — superseded: deterministic density cap removed; showrunner prompt handles curation at LLM level
- [ ] **P2**: Improve claim-level multi-source diversity via post-build merge/corroboration or richer builder prompting, instead of leaving almost all planning claims at `source_diversity = 1`

**Round 74 (act-planner claim-first evidence packaging):**
- [x] **P0**: Remove flat `evidence[:20]` payload capping from `act_planner`; scene planning now receives claim-first support bundles instead of a lossy global evidence list
- [x] **P0**: Add deterministic claim-first evidence hydration for beat scenes before `vector_search` rescue, so claim-anchored scenes reuse bundled support from the act payload
- [x] **P1**: Reduce act-planner payload noise by dropping flat evidence/provenance blobs and keeping compact claim bundles (`type`, support counts, branch/intents, `support_bundle`)
- [x] **P1**: Budget claim support in prompt packaging as `1` best direct quote per claim plus `1` optional illustrative quote, instead of sending an unstructured evidence bag

**Round 75 (screenwriter continuity handoff):**
- [x] **P0**: Trim `screenwriter` payload back to scene-level continuity context (`act_title`, `previous_scene`, `transition_note`) instead of full act-contract duplication
- [x] **P1**: Add explicit screenwriter language lock and anti-explainer prompt rules so dialogue stays in the podcast language and does not collapse into generic Host A explains / Host B asks loops
- [x] **P1**: Add local written-claim memory so scenes can callback to already covered material inside the same act

**Round 76 (screenwriter cross-act continuity + dialogue flexibility):**
- [x] **P0**: Add screenplay-level covered-claim memory and `previous_act_summary` so later acts stop resetting already explained mechanisms
- [x] ~~**P1**: Add `new_information_goal` to the writer payload~~ — superseded: removed and replaced by `prior_scenes_dialogue` + `callback_claims`
- [x] **P1**: Loosen host-role rigidity, discourage strict `ABAB` alternation / repeated opener phrases, and add light source-aware attribution guidance for precise dates, rules, and figures

**Round 77 (integrator reliability hardening):**
- [x] **P1**: Raise `integrator` structured-output budget, add strict JSON retry, and stop relying on a single `2048`-token pass that truncates long transition diagnostics
- [x] **P1**: Add `planning_integrator` debug dumps so integration failures are inspectable instead of degrading into silent no-ops
- [x] **P1**: Add regression tests for integrator prompt/output contract and missing-beat-sheet validation helper

**Round 78 (act-planner novelty split + writer mechanism continuity):**
- [x] **P0**: Deterministically split repeated scene claim usage into `primary_claim_ids` and `callback_claim_ids` by normalized scene order, instead of reteaching the same claim as primary in multiple scenes
- [x] **P0**: Add a narrow act-planner overlap gate: callback-only `hook` / `explanation` / `case_study` scenes now fail the attempt and use the bounded `ACT_PLANNER_RETRIES = 2` retry budget
- [x] **P1**: Keep writer scene prompts focused on `primary_claim_ids`, while manifest/debug continuity still preserves primary+callback claim coverage for downstream steps
- [x] ~~**P1**: Add generic `mechanism_anchors` to global covered-claim memory~~ — superseded: `mechanism_anchors` removed; continuity now handled by `prior_scenes_dialogue` and `already_covered_claims_global`
- [x] **P1**: Add regression tests for normalized-order split, callback-only scene gating, manifest claim aggregation

**Round 79 (screenwriter callback-aware continuity):**
- [x] **P0**: Add `scene_claim_mode` and full `callback_claims` objects to the writer payload so callback-heavy scenes do not behave like fresh exposition scenes
- [x] **P0**: Replace planning-brief `previous_scene` handoff with deterministic continuity extracted from the actual written previous scene (`what_listener_now_knows`, `claim_ids`, `last_line`)
- [x] **P1**: Teach screenwriter to use `previous_scene.last_line` as a natural conversational bridge when the next scene can continue without a hard reset
- [x] **P1**: Add regression tests for callback-aware scene payloads, written-dialogue previous-scene continuity, and helper extraction for dialogue texts

**Round 80 (scene jobs + prior primary memory):**
- [x] **P0**: Add `scene_job` to act-planner beat sheets for callback/mixed scenes so writer sees a positive move (`reframe`, `cost`, `contrast`, `bridge`, `synthesize`, `apply`, `stress_test`) instead of only a negative anti-repetition rule
- [x] **P0**: Add `prior_acts_primary_claims` to later act-planner payloads so new acts know what earlier acts already established as primary explanation material
- [x] **P1**: Tighten callback-only ending constraints in act planning with structural limits on claim count and ending-friendly `scene_job`, while keeping overlong ending descriptions advisory-only instead of brittle hard-fail proxies
- [x] **P1**: Add `scene_job` and deterministic `recent_scene_starts` to screenwriter payloads so callback scenes know what move to make and scene openers repeat less inside one act
- [x] **P1**: Widen previous-scene continuity slices for writer and add regression tests for prior-primary planner memory, scene-job defaults, callback-only ending gating, and recent scene starts

**Round 81 (runtime hardening — stalled planning visibility):**
- [x] **P1**: Add shared hard timeout for Anthropic-backed LLM requests (`LLM_REQUEST_TIMEOUT_SECONDS`, default `300`) so generation workers fail fast on stalled provider calls instead of hanging indefinitely
- [x] **P1**: Act planner now writes an early `started` debug dump before the first LLM call, so step-mode reruns leave inspectable artifacts even when the provider request stalls or times out
- [x] **P2**: Add regression coverage for the shared LLM timeout helper and the act-planner timeout debug-dump path

**Round 82 (act-planner retry budget split):**
- [x] **P1**: Split `act_planner` retries into separate budgets for request/output failures vs structural quality-gate failures, so provider timeouts do not consume the last retry reserved for beat-sheet repair
- [x] **P1**: Only attach `previous_error` feedback on retries that returned usable-but-bad output; transport timeouts now retry with the same prompt instead of adding noisy error prose
- [x] **P2**: Add regression test for `timeout -> quality failure -> success` to lock the new retry semantics

**Round 83 (prompt-level semantic novelty tightening):**
- [x] **P0**: Strengthen `showrunner` prompt so semantic novelty outranks branch coherence when near-paraphrase claims from different research branches would otherwise become fresh explanatory backbone in multiple acts
- [x] **P0**: Strengthen `act_planner` prompt so later acts must add a genuinely new layer (`stress_test`, `cost`, `contrast`, legal framing, consequence) instead of restating an already established mechanism in new words
- [x] **P1**: Tighten callback-heavy beat-card instructions so endings lead with the new move first, rather than opening with a recap of the old mechanism

**Round 84 (prompt-level scene differentiation and chronology):**
- [x] **P0**: Strengthen `showrunner` prompt so one historical event or institutional decision usually gets one main dramatic act home, instead of being half-revealed in one act and re-revealed in another
- [x] **P0**: Strengthen `act_planner` prompt so adjacent scenes cannot walk through the same procedural chain twice under different labels; scene N+1 must do something with the chain rather than re-explain it
- [x] **P1**: Add prompt guidance that historical acts should stay chronologically legible by default, `cost` should only be used for real tradeoffs, and `contrast` beats should stay selective instead of importing multiple unrelated outside mini-histories

**Round 85 (opaque short IDs across LLM-facing contracts):**
- [x] **P0**: Claim Builder now shortens LLM-facing `evidence_ids` and restores them before persistence, so evidence-link assignment no longer depends on retyping raw UUIDs
- [x] **P0**: Act Planner now shortens both `claim_ids` and bundled `evidence_ids` in prompt/output contracts and restores them after parse
- [x] **P1**: `dialogue_polish` and citation repair now shorten LLM-facing `claim_ids` / `available_claims` and restore them after parse, so phase-6 repair layers stop leaking UUID-typo risk downstream
- [x] **P1**: Added regression coverage for short-id prompt payloads and restore semantics in claim builder, act planner, writer revolt, and citation auditor

**Round 86 (claim-builder rerun hygiene + semantic dedupe follow-up):**
- [x] **P0**: Targeted `build_claims` reruns now revalidate preserved claims against the current live branch/question scope and fall back to `full_rebuild` when the old claim pool no longer matches the current tree
- [x] **P0**: `dedupe_claims` now sanitizes incoming `state["claim_ids"]` against live DB rows, reports requested vs loaded vs missing counts, and stops treating stale historical IDs as part of the live merge pool
- [x] **P1**: Claim Builder branch context is now split into primary vs secondary/provenance contexts so cross-branch lineage stays visible without silently becoming branch bleed
- [x] **P1**: Claim Builder prompt now keeps `one downstream move per claim` instead of rewarding maximum compression into setup+response / regime-shift mini-syntheses
- [x] **P1**: Semantic dedupe now uses overlapping sorted windows and explicit prompt guardrails for problem->answer / regime->later transition / legal rule->later application pairs

**Round 87 (claim-builder UX rerun contract + conservative merge bias):**
- [x] **P0**: Manual step reruns of `build_claims` now force `full_rebuild` semantics and replace the prior claim pool instead of preserving untouched branches like an internal verifier-loop patch
- [x] **P1**: Claim Builder prompt no longer says `prefer merge when unsure`; ambiguous shapes now bias toward narrower/split claims, with contrastive bad/good examples for compressed mini-scene claims
- [x] **P1**: Cross-branch semantic dedupe now prefers false negatives over false positives by filtering weak/ambiguous merge suggestions after the Haiku pass

**Round 88 (cross-branch dedupe observability + no veto):**
- [x] **P1**: Removed the extra deterministic cross-branch merge veto; semantic dedupe now applies Haiku merge groups directly
- [x] **P1**: Dedupe debug now records cross-branch suggested/applied/skipped merges so reruns show whether the model proposed a merge and what actually happened
- [x] **P1**: Cross-branch dedupe now consolidates overlapping-window suggestions before apply, preventing opposite-direction merge cycles while keeping raw-vs-consolidated debug visible
- [x] **P1**: Dedupe debug now records rerun input origin (`pipeline_state_claim_ids` vs `checkpoint_pre_node_state`) alongside `requested/loaded/missing` counts, so isolated reruns make checkpoint-vs-live-pool mismatch explicit

Deferred (by design):
- **P2**: OAuth endpoints (using dev-login for development)
- **P2**: Missing frontend pages (episodes list, register) — low priority
- **P2**: Full web cascade (FireCrawl + Playwright) — trafilatura + Jina + httpx sufficient for MVP
- **P2**: tus uploads, wavesurfer.js — nice-to-have UX features
