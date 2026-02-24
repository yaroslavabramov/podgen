# Podgen Pipeline I/O Map (Code Reality)

Дата фиксации: 2026-02-24  
Источник истины: текущий код в `backend/app/**` + фактические entrypoints API/worker.  
Назначение: дать инженерную карту по шагам — что на входе, что на выходе, какие сервисы/тулы вызываются, где циклы и ограничения.

## 1. Scope и источник истины

Документация, с которой сверялась карта:
- `README.md`
- `PIPELINE.md`
- `IMPLEMENTATION.md`
- `DEVELOPMENT_RULES.md`
- `CLAUDE.md`
- `TODO.md`

Кодовые точки истины:
- API запуск генерации: `backend/app/api/generations.py:26`
- API загрузки источников: `backend/app/api/sources.py:32`, `backend/app/api/sources.py:64`
- Worker генерации: `backend/app/workers/generation_tasks.py:7`
- Worker ingestion: `backend/app/workers/ingestion_tasks.py:11`
- Ingestion router: `backend/app/ingestion/router.py:23`
- LangGraph сборка: `backend/app/pipeline/graph.py:60`
- Контракт state: `backend/app/pipeline/state.py:4`

## 2. End-to-end поток

### 2.1 Поток ingestion (Phase 1, отдельно от LangGraph)
1. Пользователь загружает файл/URL через API (`/sources/upload` или `/sources/url`).
2. Создаётся `Source` со статусом `pending`.
3. Дёргается Celery-task `ingest_source.delay(source_id)`.
4. Worker:
   - переводит `Source` в `processing`,
   - выполняет extraction -> normalization -> chunking -> embeddings -> canonicalization -> content map,
   - ставит `completed|failed`.

### 2.2 Поток generation (Phases 0,2-7 в LangGraph)
1. `POST /api/projects/{id}/generate` создаёт `GenerationRun(status=queued)`.
2. Дёргается Celery-task `run_generation_pipeline.delay(run_id)`.
3. Worker:
   - переводит run в `running`,
   - вызывает `execute_pipeline(...)`,
   - после успеха -> `completed`, при исключении -> `failed`.
4. `execute_pipeline` строит initial state (только IDs), запускает graph с Postgres-checkpointer.
5. На выходе валидируется целостность эпизода (audio path, non-empty transcript, duration > 0).

### 2.3 Realtime
- Узлы публикуют события в Redis channel `generation:{run_id}`.
- WebSocket `/api/ws/generations/{run_id}` читает pub/sub и пушит клиенту.

## 3. Runtime компоненты и внешние зависимости

| Компонент | Где используется | Назначение |
|---|---|---|
| PostgreSQL | `models/*`, все pipeline nodes | Хранилище артефактов по фазам |
| Redis | `realtime/publisher.py`, Celery broker/backend | Pub/sub + очередь задач |
| Celery | `workers/celery_app.py` | Async ingestion/generation/deep-ingestion |
| LangGraph + Postgres checkpointer | `pipeline/graph.py` | Оркестрация фаз 0,2-7 и resume |
| MinIO | `services/storage.py` | Raw texts, source files, episode audio |
| Qdrant | `services/embeddings.py` | Векторный индекс chunks |
| Anthropic Sonnet/Haiku | `services/llm.py` | Reasoning/writing/classification |
| OpenAI embeddings | `services/llm.py:get_embeddings` | Embeddings для Qdrant |
| Tavily | `phase3_research/tools.py:web_search_tool` | Open-web search |
| Jina Reader | `ingestion/web.py`, `phase3_research/tools.py:fetch_page_tool` | Web extraction |
| Deepgram | `ingestion/video.py` | Transcription audio/video |
| ElevenLabs | `phase7_audio/tts.py` | TTS |
| pydub/ffmpeg | `phase7_audio/audio_processor.py` | Post-processing audio |

## 4. State контракт (только IDs)

Фактический `PipelineState`:
- `generation_run_id`
- `project_id`
- `user_id`
- `user_prompt`
- `duration_target`
- `language`
- `show_bible_id`
- `source_ids`
- `content_map_id`
- `manifest_id`
- `evidence_ids`
- `claim_ids`
- `conflict_brief_ids`
- `script_segment_ids`
- `episode_id`
- `current_phase`
- `iteration_counts`
- `quality_signals`

Источник: `backend/app/pipeline/state.py:4`.

## 5. Phase 1 (Ingestion) — детальный I/O

### 5.1 Триггеры
- `POST /api/projects/{project_id}/sources/upload` -> создаёт `Source(pending)` -> `ingest_source.delay(...)` (`backend/app/api/sources.py:32-61`).
- `POST /api/projects/{project_id}/sources/url` -> то же (`backend/app/api/sources.py:64-82`).
- TUS-resumable upload тоже заканчивается созданием `Source(pending)` + `ingest_source.delay(...)` (`backend/app/api/sources.py:414+`).

### 5.2 Ingestion task
`backend/app/workers/ingestion_tasks.py`
- Вход: `source_id` (`ingest_source`, строка 11).
- Поведение:
  - DB retry до 5 раз для SQLAlchemy ошибок (`11-30`).
  - `source.ingestion_status = processing` (`44-45`).
  - `run_ingestion(source, db)` (`48-49`).
  - success -> `completed`, `fully_ingested=True`, очищение `metadata.error` (`51-55`).
  - error -> `failed`, запись `metadata.error` (`56-60`).

### 5.3 Ingestion pipeline (`run_ingestion`)
`backend/app/ingestion/router.py:23`

Вход:
- ORM `Source` (`source_type`, `file_path/original_url`, `project_id`, `id`).

Шаги:
1. Extraction:
- PDF -> `extract_pdf` (`39-44`, `ingestion/pdf.py:12`), Marker/PyPDF fallback.
- WEB -> `extract_web` (`45-49`, `ingestion/web.py:52`), Jina -> httpx HTML strip fallback.
- VIDEO/AUDIO -> `extract_video` (`50-51`, `ingestion/video.py:12`), Deepgram `nova-3`.

2. Raw text storage:
- upload raw text в MinIO `raw_text/{project_id}/{source_id}.md` (`59-63`).

3. Normalization:
- `normalize_text` (`65-66`, `ingestion/normalize.py:16`) — Unicode/whitespace + PDF-specific cleanup.

4. Chunking:
- `structural_chunk` (`76-78`, `ingestion/chunker.py:44`) — H2/H3 split, merge/split by token budget, overlap 64.

5. Embeddings + index:
- `index_chunks` (`84-102`, `services/embeddings.py:29`) -> OpenAI embeddings + upsert в Qdrant.

6. Canonicalization:
- `canonicalize_sources` (`104-109`, `ingestion/canonicalizer.py:124`) — dedup, reliability, published_at extraction.

7. Content map rebuild:
- `build_content_map` (`111-116`, `ingestion/scout.py:23`) — Haiku structured summary.

Выход:
- `Source`: `raw_text_path`, `text_preview`, статусы ingestion.
- `Chunk` rows + `qdrant_point_id`.
- `ContentMap` row.

### 5.4 Deep ingestion task
`backend/app/workers/ingestion_tasks.py:81`
- Вход: `source_id`, `generation_run_id`.
- Берёт lock `SELECT ... FOR UPDATE SKIP LOCKED` (`99-103`).
- Выполняет `run_ingestion`.
- Успех: `is_temporary=False`, `fully_ingested=True`, `completed`, `deep_ingestion_completed` event (`118-125`).
- Ошибка: `failed` (`126-128`).

## 6. LangGraph pipeline (Phases 0,2-7) — по нодам

Граф и порядок: `backend/app/pipeline/graph.py:65-110`.

### Phase 0 — `setup_show`
Файл: `phase0_setup/show_configurator.py:185`

- State input:
  - `generation_run_id`, `show_bible_id?`, `user_prompt`, `project_id`.
- DB read:
  - `GenerationRun`, optional `ShowBible`.
- External:
  - `call_sonnet_structured` для ShowBible generation (`155-160`).
  - `parse_llm_json` + payload validation (`165-182`, `90-136`).
- DB write:
  - создаёт `ShowBible`, пишет `GenerationRun.show_bible_id` (`231-249`).
- State output:
  - `show_bible_id`, `current_phase=phase0_setup`.
- Events:
  - `phase_started`, `step_started/completed`, `phase_completed`, progress update.

### Phase 2 — `decompose`
Файл: `phase2_decomposition/decomposer.py:58`

- State input:
  - `generation_run_id`, `content_map_id`, `show_bible_id`, `user_prompt`, `duration_target`, `language`.
- DB read:
  - `ContentMap`, `ShowBible`, optional existing `EpisodeManifest`.
- External:
  - `call_sonnet_structured` для research tree (`103-108`).
  - `compute_fill_layers` (`112-117`, `duration_strategy.py:29`).
- DB write:
  - create/update `EpisodeManifest` (`126-141`).
- State output:
  - `manifest_id`, `current_phase=phase2_decomposition`.
- Events:
  - phase + steps + progress.

### Phase 3.1 — `hunt_closed_book`
Файл: `phase3_research/hunter.py:40`

- State input:
  - `generation_run_id`, `source_ids`, `manifest_id`, `evidence_ids`, `project_id`.
- DB read:
  - `EpisodeManifest`, chunks/sources через tools.
- External/tools:
  - `vector_search_tool` -> Qdrant (`78`, tools.py `15-33`).
  - `keyword_search_tool` -> PG FTS (`79`, tools.py `36-66`).
  - `get_chunk_content` (`91`, tools.py `69-90`).
  - `call_haiku_structured` для extraction (`106-113`).
- DB write:
  - `EvidenceSpan` rows (`135-152`).
- State output:
  - `evidence_ids`, `current_phase=phase3_research`.
- Events:
  - `phase_started`, `step_started/completed`, `evidence_found`, progress.

### Phase 3.2A — `research_fast_web`
Файл: `phase3_research/librarian_fast.py:41`

- State input:
  - `generation_run_id`, `manifest_id`, `evidence_ids`, `iteration_counts`, `project_id`.
- DB read:
  - `EpisodeManifest`, temp `Source` для deep candidates.
- External/tools:
  - `web_search_tool` (Tavily) (`87`, tools.py `93-107`).
  - `fetch_page_tool` (Jina + SSRF check) (`95`, tools.py `109-133`).
  - `call_sonnet_structured` для evidence extraction (`114-120`).
  - `create_web_evidence` (создаёт temp Source + EvidenceSpan) (`126-133`, tools.py `135-165`).
  - `trigger_deep_ingestion` (`180`, librarian_deep.py `10-15`).
- DB write:
  - новые temp `Source`, `EvidenceSpan`, статусы temp source `ENQUEUED`.
- State output:
  - `evidence_ids`, `iteration_counts.fast_web_per_act`, `iteration_counts.deep_ingestion_active`, `current_phase`.
- Governors:
  - `max_fast_web_per_act` (`51-53`, config `63`).
  - `max_deep_ingestion_concurrent` (`163`, config `64`).

### Phase 4.1 — `build_claims`
Файл: `phase4_claims/claim_builder.py:48`

- State input:
  - `generation_run_id`, `evidence_ids`, `project_id`, `language`.
- DB read:
  - `EvidenceSpan`.
- External:
  - `call_sonnet_structured` + `parse_llm_json` (`94-101`).
- DB write:
  - `Claim`, `ClaimEvidence` (`106-154`).
- State output:
  - `claim_ids`, `current_phase=phase4_claims`.

### Phase 4.2 — `verify_claims`
Файл: `phase4_claims/verifier.py:22`

- State input:
  - `generation_run_id`, `claim_ids`, `iteration_counts`.
- DB read:
  - `Claim`, `ClaimEvidence`, `EvidenceSpan`, `Source`.
- External:
  - Нет обязательных внешних вызовов; verification rule-based.
- DB write:
  - `claim.verification_level`.
- State output:
  - `claim_ids`, `current_phase`, `iteration_counts.verify_research_loops`, `iteration_counts.verify_needs_research`.
- Loop signals:
  - Если есть `UNVERIFIED fact` -> `verify_needs_research=True`.

### Phase 4.3 — `resolve_conflicts`
Файл: `phase4_claims/conflict_resolver.py:33`

- State input:
  - `generation_run_id`, `claim_ids`, `conflict_brief_ids`, `project_id`.
- DB read:
  - `Claim`.
- External:
  - `call_sonnet_structured` (`65-72`).
- DB write:
  - `ConflictBrief` (`73-84`).
- State output:
  - `conflict_brief_ids`, `current_phase`.

### Phase 5.1 — `plan_episode`
Файл: `phase5_planning/showrunner.py:54`

- State input:
  - `generation_run_id`, `show_bible_id`, `claim_ids`, `conflict_brief_ids`, `manifest_id`, `duration_target`.
- DB read:
  - `ShowBible`, `Claim`, `ConflictBrief`, `EpisodeManifest`.
- External:
  - `call_sonnet_structured` + normalization (`113-115`, `161+`).
- DB write:
  - `EpisodeManifest.act_count`, `EpisodeManifest.acts_json` (`123-126`).
- State output:
  - `manifest_id`, `current_phase`.

### Phase 5.2 — `plan_act`
Файл: `phase5_planning/act_planner.py:54`

- State input:
  - `generation_run_id`, `manifest_id`.
- DB read:
  - `EpisodeManifest`, `Claim`, `ClaimEvidence`, `EvidenceSpan`.
- External:
  - `call_sonnet_structured` per act with retries (`129-159`).
- DB write:
  - дописывает `beat_sheet` в `EpisodeManifest.acts_json` (`166-177`).
- State output:
  - `manifest_id`, `current_phase`.
- Retry:
  - `ACT_PLANNER_RETRIES=2`.

### Phase 5.3 — `integrate_acts`
Файл: `phase5_planning/integrator.py:40`

- State input:
  - `generation_run_id`, `manifest_id`.
- DB read:
  - `EpisodeManifest`.
- External:
  - `call_sonnet_structured` integration check (`81-82`).
- DB write:
  - transition notes в `acts_json` (`84-93`).
- Side behavior:
  - при пустых beat_sheet делает recovery через `plan_act(state)` (`59-66`).
- State output:
  - `manifest_id`, `current_phase`.

### Phase 6.1 — `write_screenplay`
Файл: `phase6_writing/screenwriter.py:55`

- State input:
  - `generation_run_id`, `show_bible_id`, `manifest_id`, `language`.
- DB read:
  - `ShowBible`, `EpisodeManifest`, existing `ScriptSegment`, `Claim`, `EvidenceSpan`.
- External:
  - `call_sonnet_structured` per scene (`210-215`) + parse/normalize.
- DB write:
  - insert/update `ScriptSegment`, commit per scene (`261-280`).
- State output:
  - `script_segment_ids`, `current_phase`.
- Retry/resume:
  - 3 попытки на сцену (`23`, `208+`), reuse старых сцен, rewrite только `AuditStatus.REVISED`.

### Phase 6.2 — `evaluate_and_revolt`
Файл: `phase6_writing/writer_revolt.py:38`

- State input:
  - `generation_run_id`, `script_segment_ids`, `iteration_counts`, `quality_signals`.
- DB read/write:
  - `ScriptSegment`: `engagement_score`, `audit_status`, `revision_count`.
- External:
  - `call_haiku_structured` (`64-75`).
- State output:
  - `script_segment_ids`, `iteration_counts.writer_planner_loops`, `_prev_revolt_count`, `quality_signals.revolt_triggered`.
- Governors:
  - `max_writer_revolt_per_act`, `max_writer_revolt_per_scene`.

### Phase 6.3 — `audit_citations`
Файл: `phase6_writing/citation_auditor.py:54`

- State input:
  - `generation_run_id`, `script_segment_ids`, `show_bible_id`, `claim_ids`, `language`, `quality_signals`.
- DB read:
  - `ShowBible`, `Claim`, `ScriptSegment`.
- External:
  - `call_sonnet_structured` (`99-100`).
- DB write:
  - `ScriptSegment.audit_status`, `ScriptSegment.quality_metrics`.
- State output:
  - `script_segment_ids`, `quality_signals`, `current_phase`.
- Nuance:
  - если аудит конкретного сегмента упал исключением -> сегмент помечается `PASSED` (`101-104`).

### Phase 7.1 — `generate_audio`
Файл: `phase7_audio/tts.py:22`

- State input:
  - `generation_run_id`, `script_segment_ids`, `show_bible_id`, `project_id`, `user_prompt`, `language`.
- DB read:
  - `ShowBible.voice_profiles`, `ScriptSegment`, optional existing `Episode`.
- External:
  - ElevenLabs (`173-193`) или silence fallback (`168-170`, `194-195`).
  - Audio post-process `process_audio` (`112`, `audio_processor.py:16`).
  - MinIO upload (`117-120`).
- DB write:
  - create/update `Episode` с `audio_file_path`, `transcript_json`, `duration_seconds` (`131-154`).
- State output:
  - `episode_id`, `current_phase`.
- Guards:
  - fail fast при пустом transcript/audio (`104-114`).

### Phase 7.2 — `build_shownotes`
Файл: `phase7_audio/shownotes.py:53`

- State input:
  - `generation_run_id`, `episode_id`, `claim_ids`, `conflict_brief_ids`, `language`, `quality_signals`.
- DB read:
  - `Episode`, `Claim`, `ClaimEvidence`, `EvidenceSpan`, `Source`, `ConflictBrief`.
- External:
  - `call_haiku_structured` (`134-135`).
- DB write:
  - `Episode.shownotes_json`, `Episode.quality_metrics` (`140-142`).
- State output:
  - `episode_id`, `current_phase`.
- Guards:
  - строгая проверка существования и целостности episode artifacts (`57-83`).

## 7. Глобальные циклы, лимиты, checkpoint/resume

### 7.1 Условные циклы графа
`backend/app/pipeline/graph.py`
- Verify loop:
  - `verify_claims` -> `research_fast_web`, если `verify_needs_research` и loop<2 (`39-46`, `91-94`).
- Writer revolt loop:
  - `evaluate_and_revolt` -> `write_screenplay`, если `revolt_triggered` и loop<=4 (`49-57`, `103-106`).

### 7.2 Governors (settings)
`backend/app/config.py:62-68`
- `max_fast_web_per_act=5`
- `max_deep_ingestion_concurrent=3`
- `max_writer_revolt_per_act=4`
- `max_writer_revolt_per_scene=2`
- `max_verifier_research_loops=2`
- `temp_source_ttl_days=7`

### 7.3 Checkpoint/resume
`backend/app/pipeline/graph.py:261-311`
- thread_id хранится в `GenerationRun.langgraph_thread_id`.
- resume запускается с `graph_input=None`.
- если checkpoint отсутствует -> runtime error.
- завершение валидируется через `_assert_episode_complete` (`188-200`).

## 8. Realtime и наблюдаемость

- Event schema: `backend/app/realtime/events.py:14-79`.
- Publish: `backend/app/realtime/publisher.py:18-29`.
- WS relay + auth/ownership guard: `backend/app/api/ws.py:48-87`.

## 9. Docs vs Code (зафиксированные различия)

Ниже только подтверждённые расхождения между `.md` и текущей реализацией.

### D1. Поле `pending_deep_ingestion_job_ids` в state
- Документация: `IMPLEMENTATION.md:567`, `IMPLEMENTATION.md:976`.
- Код: в `PipelineState` поля нет (`backend/app/pipeline/state.py:4-22`), deep ingestion трекается через DB status и `iteration_counts.deep_ingestion_active` (`backend/app/pipeline/phase3_research/librarian_fast.py:153-189`).
- Влияние: docs описывает state contract старой версии.

### D2. Web ingestion cascade (FireCrawl/Playwright)
- Документация: `IMPLEMENTATION.md:708`.
- Код: Jina -> httpx fallback, без FireCrawl/Playwright (`backend/app/ingestion/web.py:55-82`).
- Доп. подтверждение: `TODO.md:26`, `TODO.md:184`.
- Влияние: ожидаемое поведение web extraction в доке шире, чем реализовано.

### D3. Deep ingestion «апгрейдит claim verification и evidence locators»
- Документация: `IMPLEMENTATION.md:964-968`, `IMPLEMENTATION.md:977`.
- Код: `deep_ingest_url` вызывает только `run_ingestion`, обновляет статус source и шлёт event (`backend/app/workers/ingestion_tasks.py:114-125`), явной логики upgrade claims нет.
- Влияние: завышенное ожидание влияния deep ingestion на текущий run.

### D4. Act planners как parallel fan-out (LangGraph Send)
- Документация: `IMPLEMENTATION.md:1105-1107`, `1125-1131`.
- Код: один node `plan_acts` без `Send`, внутри обычный последовательный цикл по актам (`backend/app/pipeline/graph.py:97-99`, `backend/app/pipeline/phase5_planning/act_planner.py:77-177`).
- Влияние: docs обещает параллелизм/кэш-координацию, в коде их нет.

### D5. Verifier как LLM+tools (`update_claim`, `request_research`)
- Документация: `IMPLEMENTATION.md:1014`, `1036-1038`.
- Код: rule-based Python алгоритм без tool-calling и без обязательного LLM шага (`backend/app/pipeline/phase4_claims/verifier.py:32-124`).
- Влияние: архитектурное описание verifier в доках не совпадает с реальностью.

### D6. Дефолт голосов «первые 2 ElevenLabs voices»
- Документация: `IMPLEMENTATION.md:627`.
- Код: если `voice_id` не задан или нет API key, TTS возвращает silence placeholder (`backend/app/pipeline/phase7_audio/tts.py:168-170`, `194-195`).
- Влияние: без явного выбора voice профилей можно получить «тихий» эпизод.

### D7. Auth endpoints в документации
- Документация: `IMPLEMENTATION.md:1423-1424` (`/api/auth/callback`, `/api/users/me`).
- Код: реализован `POST /api/auth/dev-login` (`backend/app/main.py:56`), указанных endpoint-ов нет.
- Влияние: API section в IMPLEMENTATION частично плановая, не фактическая.

### D8. Output ingestion metadata (author/page_count/duration/...)
- Документация: `IMPLEMENTATION.md:714`.
- Код: `run_ingestion` устанавливает `raw_text_path`, `text_preview`; структурированное metadata заполняется в основном при ошибках (`backend/app/ingestion/router.py:59-72`, `backend/app/workers/ingestion_tasks.py:53-59`).
- Влияние: поле `Source.metadata` сейчас не является стабильным контрактом extraction metadata.

### D9. Deepgram language mode
- Документация: `IMPLEMENTATION.md:709` указывает `language="auto"`.
- Код: жёстко задано `language="en"` (`backend/app/ingestion/video.py:29-35`).
- Влияние: авто-детект языка для media ingestion сейчас не реализован.

### D10. «Act Planner может делать request_fast_research/search_evidence как tool calls»
- Документация: `IMPLEMENTATION.md:1121-1123`, `1130-1131`.
- Код: `plan_act` использует только уже существующие claims/evidence и не вызывает research tools (`backend/app/pipeline/phase5_planning/act_planner.py:86-125`, `127-177`).
- Влияние: planner не дообогащает evidence в фазе 5 напрямую.

## 10. Что обновлять в документации в первую очередь

1. `IMPLEMENTATION.md`:
- секции state contract (Phase 3 outputs),
- web ingestion cascade,
- deep-ingestion side effects,
- phase 5 parallelism,
- verifier description,
- auth endpoints,
- voice defaults,
- media language mode.

2. `PIPELINE.md`:
- явно отметить, что часть описания — target architecture, а не текущая code reality.

3. `README.md`:
- добавить ссылку на этот файл как «операционный контракт текущей реализации».

