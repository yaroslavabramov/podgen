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
| 6 | Writing | Screenwriter generates dialogue, dialogue audit/polish improves flow, citation audit/repair enforces rigor before audio |
| 7 | Audio | TTS generation, audio processing, shownotes |

Key principle: **State-by-IDs** — LangGraph state holds only ID references, never full texts. Each node loads from DB and writes back.

Non-negotiable product constraint:

- Podgen is a universal multilingual system that must work across very different source corpora.
- Do not solve pipeline problems with case-specific hacks, hardcoded phrase dictionaries, language-specific wordlists, or one-off fixes tuned to a single episode.
- User- or project-defined constraints such as `forbidden_phrases` in the ShowBible are valid. System-level behavior must be driven by structured metadata, prompt contracts, issue types, and acceptance logic that generalize across languages and corpora.
- If a proposed fix depends on recognizing a small hand-built list of phrases in one language, treat it as the wrong abstraction by default.

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
- targeted rebuild now reuses prior verification state/fingerprint for semantically unchanged rebuilt claims, revalidates preserved claims against the current live branch/question scope before carrying them forward, and falls back to full rebuild when the old claim pool no longer matches the current tree; manual step reruns of `build_claims` also force a fresh rebuild that replaces the prior claim pool instead of quietly behaving like an internal incremental patch
- `build_claims` now also writes a compact pre-dedupe claim snapshot into pipeline state, so isolated `dedupe_claims` reruns can reconstruct claims that an earlier dedupe pass already hard-deleted and still rerun against the last builder output
- targeted rebuild scope now expands merged verifier gaps across all participating `claim_ids` and branch/question contexts before rebuilding
- claim builder now budgets against real prompt-quote lengths and uses adaptive long-quote packing instead of a blind `[:700]` prefix cut
- claim builder prompt now optimizes for a compact writer-usable backbone: it prefers fewer stronger claims, keeps `one downstream move per claim`, avoids `one evidence span -> one micro-claim`, separates primary branch context from secondary/provenance-only question context so cross-branch lineage does not silently become branch bleed, and now prefers narrower/split claims over compressed mini-scene claims when merge shape is ambiguous
- `dedupe_claims` now sanitizes the incoming pool before any merge logic, reporting requested vs loaded vs missing claim IDs in debug output instead of treating stale historical state as a live pool
- semantic dedupe now runs on overlapping sorted Haiku windows, improving cross-batch duplicate coverage without returning to an all-pairs cost explosion; both per-branch and cross-branch semantic apply enforce `claim_type` match and numeric/date anchors (`numbers_and_dates`) match, otherwise merges are skipped with explicit `skip_reason` in debug; cross-branch additionally consolidates overlapping-window suggestions before apply; debug output records raw suggestions, consolidated groups, applied merges, rerun input origin (`pipeline_state_claim_ids` vs `checkpoint_pre_node_state`), plus any claim rows restored from the last `build_claims` snapshot before rerun
- LLM-facing opaque IDs are now shortened wherever the model must reproduce them: claim builder shortens `evidence_ids`, act planner shortens both `claim_ids` and `evidence_ids`, and phase-6 polish / citation-repair passes shorten `claim_ids`; outputs are restored and validated before persistence so UUID typos stop leaking downstream
- Hunter extraction now retries failed/invalid extraction passes with a narrower payload and tracks `expected_evidence` hits explicitly in debug/summary output
- fast-web page retention now filters bibliography-like windows, records explicit drop reasons, and uses authority-aware retention plus weak-source-family penalties before extractor spend
- planning payloads now carry claim-level narrative/support signals, and `act_planner` no longer depends on a flat lossy `evidence[:20]` bag
- Hunter retrieval and Claim Builder instructions are intent-aware for `debate` / `compare` / `case_study`, with stricter guardrails against claim overshoot from partial or contextual evidence
- showrunner prompt now carries explicit claim-load balance guidance (`total_claim_count`, per-act ceiling formula, duration-proportional staging), while post-normalization keeps that ceiling advisory only: pathological overload is logged for debugging, but claims are no longer moved across act boundaries or dropped without an LLM re-plan; disputed/conflict claims are still validated post-hoc and patched if the LLM omits a debate slot
- showrunner now plans explicit act story contracts (`story_role`, `purpose`, `internal_arc`, `opening_question`, `ending_turn`, `focus_branch_ids`) so planning works from editorial intent and act movement, not only from grouped claim IDs
- planning payloads now carry `output_language`, so showrunner and act planner keep human-readable output in the podcast language instead of drifting into English
- showrunner prompt now uses a role-first split/merge contract: it should decide distinct act jobs before assigning claims, treat `story_role` as listener-work rather than topic labeling, and keep provenance metadata lightweight by stripping `question_ids` from the showrunner payload while leaving `branch_ids` as a weak anchoring hint
- act planner now uses the act story contract, branch-focus summary, scene-count guidance, prior-act primary-claim memory, and claim-first support bundles to build compact beat cards instead of long pseudo-script scenes; it asks the model to declare a short `scene_strategy`, carries explicit `scene_job` for callback-heavy beats, explicitly warns against the default `hook -> explanation -> case_study -> takeaway` skeleton, treats `case_study` as optional rather than mandatory, tells later acts to add a genuinely new layer instead of rewording an already established mechanism, forbids adjacent scenes from re-walking the same procedural chain under different labels, keeps historical acts chronologically legible by default, constrains scene descriptions to compact beat cards rather than mini-drafts, validates full descriptions in the Haiku grounding gate without mid-sentence truncation, reuses bundled claim evidence before falling back to vector-search rescue, and now sends overlap retries back with explicit merge / callback / different-move guidance instead of just asking for a rewording
- Anthropic-backed LLM calls now have a shared hard request timeout (`LLM_REQUEST_TIMEOUT_SECONDS`, default `300`) so stalled provider calls fail explicitly instead of hanging Celery workers indefinitely
- integrator now uses a stricter structured-output contract with retry and debug dumps, so long transition diagnostics do not silently truncate into a no-op at `2048` tokens
- screenwriter now receives a trimmed scene-writing payload (`act_title`, `previous_scene`, `previous_act_summary`, `prior_scenes_dialogue`, local/global covered claims, `transition_note`) instead of the full act contract; `previous_scene` and `previous_act_summary` are derived from the actual written dialogue when available, so continuity follows emitted conversation instead of only planning metadata; scene payloads expose `scene_claim_mode`, `scene_job`, full `callback_claims` objects (not bare UUIDs), and deterministic `recent_scene_starts`; writer output `claim_ids` are validated post-hoc and invalid UUIDs are dropped with a warning
- screenwriter prompt now treats host roles as soft tendencies, discourages strict `ABAB` alternation and repeated opener phrases, explicitly pushes scenes to advance rather than echo the previous punchline, varies agreement/challenge discourse shapes without word blacklists, requires canonical `A/B` speaker IDs, runtime-normalizes host-name / role aliases from `ShowBible.hosts` back to `A/B`, and allows light source-aware attribution for important dates, rules, and figures
- screenwriter / dialogue polish / citation repair now keep spoken lines in the podcast language by contract: foreign-language evidence is paraphrased or translated into the dialogue language unless the wording itself is explicitly the point of the beat
- `ScriptSegment` now stores structured `audit_findings` JSON alongside numeric `quality_metrics`, so phase 6 keeps reusable issue logs instead of only scalar verdicts
- writer revolt is now a boundary-aware dialogue audit: it sees the current scene plus compact previous-scene tail context, persists structured issues with severity/scope, carries explicit `language` through audit/polish payloads, can polish scenes inline for flow/naturalness while preserving spoken register for that language, and only falls back to `audit_status = REVISED` when a full screenwriter rewrite is justified
- writer revolt now audits the whole episode first, spends polish budget per act instead of per run, and no longer starves late-act scenes behind early-scene interventions
- act-level dialogue audit is now a real full-act Haiku pass: each act is judged from full scene dialogue plus structured context (`scene_job`, turn-pattern counts, opener/closer counts, local issue types), so repeated opener mechanisms and explanatory resets are prioritized from actual act text instead of edge-only scene summaries
- writer revolt also runs one compact episode seam audit across act boundaries, so replay problems like “new act re-asks the same question” can be targeted on the opening scene of the next act instead of staying invisible to per-act analysis
- dialogue polish acceptance is now target-aware: opener-targeted and seam-targeted interventions must materially change the opener, and speaker-distribution interventions are rejected if they leave the opener in the same dominant-speaker skew
- dialogue polish is now metadata-safe by contract: the polish prompt returns `emotion` / `ssml_hints`, same-length rewrites get metadata backfilled safely, and changed-length rewrites are accepted only when the model itself preserves usable delivery metadata
- dialogue polish and citation repair now share the same speaker canonicalization contract as screenwriter: prompts demand `A/B` only, and runtime normalization maps configured host names / role aliases back to `A/B` so named-host outputs do not silently collapse to the wrong speaker
- `ScriptSegment` now also stores `writer_original_dialogue_json`, so downstream phase 6 repairs can mutate `dialogue_json` without losing the last screenwriter baseline for that scene
- citation auditor now uses Haiku for detection on scene-local claim subsets (dialogue claim ids + manifest-assigned scene claims), applies a cheap post-filter so non-assertive question lines do not become false blockers, tries mechanical `line_repair` before escalating to whole-scene repair, retries transient audit failures once before failing closed, and stops the run at the first unresolved citation blocker instead of spending through the rest of the episode
- citation audit/repair now receive explicit `language` in their payloads and use it as part of the prompt contract; repair prompts remain voice-preserving and scene-aware, prefer claim-link / local hedge / light narrowing over content deletion, know whether a scene is final inside the act/episode, preserve agency and dramatic structure, use contrastive anti-kanzelarit examples, and downgrade `unverified_stated_as_fact` only on a tiny whitelist of genuinely generic questions instead of sending them into pointless repair; recap/confirmation questions and loaded wh-questions remain actionable
- citation audit issues can now carry structured non-assertive signals such as `line_function` and `action_required`, so question handling is driven by the same multilingual audit call instead of by system-level phrase lists
- those structured signals now also affect non-question transitions/synthesis lines and the actual pass/repair/blocking decisions, so `action_required=false` is not just logged but enforced
- accepted `scene_repair` now preserves `emotion` / `ssml_hints` metadata when lines remain textually the same (or only claim links change), instead of silently flattening audio/rendering cues
- citation repair is now line-role-aware: opener / landing / punchline lines bias toward `line_repair`, unflagged sensitive lines are protected from `scene_repair`, and non-blocking acceptance compares changed/relevant lines instead of requiring noisy full-scene re-audit stability
- `available_claims` for citation audit/repair now include attribution/source-support metadata, and the auditor can flag `unsupported_source_framing` when a repair invents a source frame that the claim support does not justify
- screenwriter now writes per-scene JSON debug dumps (`tmp/debug/screenwriter`) with input payload, attempt history, raw/parsed LLM output, and final dialogue, so phase 6 regressions can be inspected directly from artifacts instead of only from final DB rows
- when debug dumps are enabled, phase 6 also writes JSON artifacts for `dialogue_audit`, `dialogue_polish`, `citation_audit`, and `citation_repair`, including findings and accept/reject decisions for repair attempts
- when `WEB_EXTRACTOR_DEBUG_DUMP_DIR` is set, Hunter / Claim Builder / Verifier / Showrunner / Act Planner write JSON debug dumps for run analysis; act planner also emits an early `started` dump before the first model call so stalled reruns leave a visible breadcrumb

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
