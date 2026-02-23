# Podgen — Детальный план имплементации

## Context

Podgen — multi-tenant SaaS для автоматической генерации long-form подкастов (15–90+ мин, 2 ведущих), на основе загруженных материалов и open-web research. Архитектура описана в `PIPELINE.md` (v2.3). Цель — полная имплементация всех 8 фаз пайплайна (0–7) с фронтендом, real-time прогрессом и продакшн-качеством.

**Ключевые принципы из PIPELINE.md:**
- **State by IDs:** в LangGraph state — только *_id списки, никаких текстов/объектов
- **Lazy loading:** тексты материализуются только в Writer/Auditor, top-K
- **Evidence-first:** факты не из саммари, всё трассируется к EvidenceSpan
- **Non-blocking web:** Deep ingestion = Celery async, никогда не блокирует акт
- **Governors:** лимиты на циклы writer↔planner, fast-web/акт, concurrent deep ingestion

---

## 1. Выбор технологий

### Backend
| Компонент | Технология | Почему |
|---|---|---|
| API Framework | **FastAPI** (Python 3.12+) | Async, типизация, OpenAPI, WebSocket |
| Agent Orchestration | **LangGraph** (0.3+) | State machines, checkpointing, fan-out (Send) |
| Checkpointing | **langgraph-checkpoint-postgres** | Resume после падений, PostgreSQL-backed |
| Task Queue | **Celery 5** + **Redis 7** | Async deep ingestion, non-blocking |
| Database | **PostgreSQL 16** + **SQLAlchemy 2** + **Alembic** | Multi-tenant, JSONB, FTS |
| Vector DB | **Qdrant** (latest) | Filtering, payload storage, hybrid search |
| Cache / Pub-Sub | **Redis 7** | Кэш + broker + pub/sub для real-time |
| File Storage | **MinIO** (S3-compatible) | PDF/audio, Docker-совместим |
| Auth | **Auth.js (NextAuth) v5** + JWT validation на бэке | OAuth, session management |
| Migrations | **Alembic** | Стандарт для SQLAlchemy |

### LLM-провайдеры (best model per task)
| Задача | Модель | Почему |
|---|---|---|
| Reasoning (decomposer, showrunner, planners, verifier) | **Claude Sonnet 4** | Баланс качества и скорости |
| Writing (screenwriter, dialogue) | **Claude Sonnet 4** | Лучшее качество текста |
| Classification / light (canonicalization, scout, engagement eval, shownotes) | **Claude Haiku 4** | Быстро, дёшево |
| Embeddings | **OpenAI text-embedding-3-large** | Лучшее качество, 3072 dims |
| Show Configurator | **Claude Sonnet 4** | Нужен reasoning для персон и стиля |

**SDK:** `anthropic` (Python SDK), `openai` (Python SDK) — прямые SDK.

### Content Ingestion
| Задача | Технология |
|---|---|
| PDF → Markdown | **Marker** (marker-pdf) |
| Web → Markdown | **Jina Reader API** → **FireCrawl** → **Playwright** (cascade) |
| Video/Audio → Text | **Deepgram** SDK (Nova-3, diarization) |
| Web Search | **Tavily** API (structured results для AI) |

### TTS & Audio
| Компонент | Технология |
|---|---|
| TTS | **ElevenLabs** SDK |
| Audio Processing | **pydub** + **ffmpeg** (нормализация, конкатенация) |

### Frontend
| Компонент | Технология |
|---|---|
| Framework | **Next.js 15** (App Router) |
| Language | **TypeScript 5** |
| UI | **Tailwind CSS 4** + **shadcn/ui** |
| State | **Zustand** |
| Real-time | **Socket.IO** client |
| Forms | **React Hook Form** + **Zod** |
| Audio Player | **wavesurfer.js** |
| File Upload | **tus-js-client** (resumable uploads) |
| HTTP Client | **ky** |
| i18n | **next-intl** (EN/RU) |

### Infrastructure (Docker Compose)
Контейнеры: `api`, `worker` (Celery), `beat` (Celery beat), `frontend`, `postgres`, `redis`, `qdrant`, `minio`

---

## 2. Структура проекта

```
podgen/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
│
├── backend/
│   ├── pyproject.toml              # uv
│   ├── alembic/
│   │   └── versions/
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory
│   │   ├── config.py               # Pydantic Settings
│   │   ├── database.py             # SQLAlchemy async engine + session
│   │   │
│   │   ├── models/                 # SQLAlchemy ORM models
│   │   │   ├── user.py
│   │   │   ├── project.py
│   │   │   ├── show_bible.py       # ShowBible + HostPersona + VoiceProfile
│   │   │   ├── source.py
│   │   │   ├── chunk.py
│   │   │   ├── evidence.py         # EvidenceSpan
│   │   │   ├── claim.py            # Claim + ClaimEvidence
│   │   │   ├── conflict_brief.py
│   │   │   ├── generation_run.py
│   │   │   ├── episode_manifest.py
│   │   │   ├── script_segment.py   # Сценарий по сценам (state-by-IDs!)
│   │   │   └── episode.py
│   │   │
│   │   ├── schemas/                # Pydantic schemas (API request/response)
│   │   │   ├── project.py
│   │   │   ├── show_bible.py
│   │   │   ├── source.py
│   │   │   ├── episode.py
│   │   │   ├── generation.py
│   │   │   └── websocket.py
│   │   │
│   │   ├── api/                    # FastAPI routers
│   │   │   ├── deps.py             # JWT validation, get_current_user
│   │   │   ├── projects.py
│   │   │   ├── show_bibles.py      # CRUD ShowBible, presets
│   │   │   ├── sources.py
│   │   │   ├── episodes.py
│   │   │   ├── generations.py
│   │   │   └── ws.py               # WebSocket endpoint
│   │   │
│   │   ├── services/
│   │   │   ├── storage.py          # MinIO
│   │   │   ├── embeddings.py       # OpenAI → Qdrant
│   │   │   ├── llm.py              # LLM client factory
│   │   │   └── cost_estimator.py   # Pre-generation cost estimate
│   │   │
│   │   ├── ingestion/              # Фаза 1
│   │   │   ├── router.py
│   │   │   ├── pdf.py
│   │   │   ├── web.py
│   │   │   ├── video.py
│   │   │   ├── normalize.py        # Text hygiene: Unicode, whitespace, PDF boilerplate
│   │   │   ├── chunker.py          # Structure-first, then normalize
│   │   │   ├── canonicalizer.py
│   │   │   └── scout.py
│   │   │
│   │   ├── pipeline/               # LangGraph (Фазы 0, 2-7)
│   │   │   ├── state.py            # PipelineState (IDs only!)
│   │   │   ├── graph.py            # Main graph assembly
│   │   │   ├── llm_json.py         # Robust JSON extraction from LLM responses
│   │   │   │
│   │   │   ├── phase0_setup/
│   │   │   │   └── show_configurator.py
│   │   │   │
│   │   │   ├── phase2_decomposition/
│   │   │   │   ├── decomposer.py
│   │   │   │   └── duration_strategy.py
│   │   │   │
│   │   │   ├── phase3_research/
│   │   │   │   ├── hunter.py
│   │   │   │   ├── librarian_fast.py    # Fast Web Evidence
│   │   │   │   ├── librarian_deep.py    # Deep Web Ingestion (Celery trigger)
│   │   │   │   └── tools.py
│   │   │   │
│   │   │   ├── phase4_claims/
│   │   │   │   ├── claim_builder.py
│   │   │   │   ├── verifier.py
│   │   │   │   └── conflict_resolver.py
│   │   │   │
│   │   │   ├── phase5_planning/
│   │   │   │   ├── showrunner.py
│   │   │   │   ├── act_planner.py
│   │   │   │   └── integrator.py
│   │   │   │
│   │   │   ├── phase6_writing/
│   │   │   │   ├── screenwriter.py
│   │   │   │   ├── citation_auditor.py
│   │   │   │   └── writer_revolt.py
│   │   │   │
│   │   │   └── phase7_audio/
│   │   │       ├── tts.py
│   │   │       ├── audio_processor.py
│   │   │       └── shownotes.py
│   │   │
│   │   ├── workers/
│   │   │   ├── celery_app.py
│   │   │   ├── ingestion_tasks.py
│   │   │   ├── worker_db.py       # Dedicated worker async session factory (NullPool)
│   │   │   └── generation_tasks.py
│   │   │
│   │   └── realtime/
│   │       ├── events.py
│   │       └── publisher.py
│   │
│   └── tests/
│       ├── conftest.py
│       ├── test_ingestion/
│       ├── test_pipeline/
│       └── test_api/
│
├── frontend/
│   ├── package.json
│   ├── next.config.ts
│   ├── tailwind.config.ts
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx                    # Landing
│   │   │   ├── (auth)/
│   │   │   │   ├── login/page.tsx
│   │   │   │   └── register/page.tsx
│   │   │   ├── (dashboard)/
│   │   │   │   ├── projects/
│   │   │   │   │   ├── page.tsx
│   │   │   │   │   └── [id]/
│   │   │   │   │       ├── page.tsx        # Project + sources + show bible
│   │   │   │   │       └── generate/page.tsx
│   │   │   │   └── episodes/
│   │   │   │       ├── page.tsx
│   │   │   │       └── [id]/page.tsx
│   │   │   └── api/auth/[...nextauth]/route.ts
│   │   │
│   │   ├── components/
│   │   │   ├── ui/                         # shadcn/ui
│   │   │   ├── source-uploader.tsx
│   │   │   ├── show-bible-editor.tsx       # Show style, hosts, audience
│   │   │   ├── generation-config.tsx
│   │   │   ├── generation-progress.tsx     # Real-time pipeline vis
│   │   │   ├── cost-estimate.tsx           # Pre-launch cost display
│   │   │   ├── episode-player.tsx
│   │   │   └── shownotes-viewer.tsx
│   │   │
│   │   ├── lib/
│   │   │   ├── api.ts
│   │   │   ├── socket.ts
│   │   │   └── auth.ts
│   │   │
│   │   ├── stores/
│   │   │   ├── generation-store.ts
│   │   │   └── project-store.ts
│   │   │
│   │   └── messages/
│   │       ├── en.json
│   │       └── ru.json
│   │
│   └── public/
│
├── PIPELINE.md
└── IMPLEMENTATION.md
```

---

## 3. Модели данных (PostgreSQL)

### User
```
id: UUID (PK)
email: String (unique)
name: String
avatar_url: String?
provider: String (google/github/email)
provider_id: String
created_at: DateTime
```

### Project
```
id: UUID (PK)
user_id: UUID (FK → User)
title: String
description: String?
language: Enum(EN, RU)
created_at: DateTime
updated_at: DateTime
```

### ShowBible
```
id: UUID (PK)
project_id: UUID (FK → Project)
audience_profile: JSONB  {level: "beginner"|"casual"|"professional", knows: [...], explain: [...]}
tone_and_mood: String  (serious/light/comedic/investigative)
show_style: String  (Investigative/Conversational/Academic Debate/Explainer)
hosts: JSONB [HostPersona]  — см. структуру ниже
voice_profiles: JSONB [VoiceProfile]  — см. структуру ниже
forbidden_phrases: String[]  (["В заключение хочу сказать...", "Как известно...", ...])
rigor_policy: Text  ("ключевые FACT в основной линии должны быть VERIFIED")
adjacency_policy: Text  ("только если усиливает core / механизм / конфликт / кейс")
explanation_policy: Text  ("термин→определение→пример; цифры→интуитивный перевод")
format_rules: JSONB  {max_reply_length, summary_frequency, humor_level, profanity_allowed}
is_preset: Boolean (false)
preset_name: String?  (для пресетов: "Investigative Duo", "Casual Explainer", etc.)
created_at: DateTime
updated_at: DateTime
```

#### HostPersona (JSONB внутри ShowBible.hosts)
```json
{
  "name": "Alex",
  "role": "Guide",              // Guide | Challenger
  "vocabulary_rules": "простые слова, без жаргона",
  "interaction_style": "резюмирует, задаёт наводящие вопросы",
  "explanation_behavior": "через аналогии из повседневной жизни",
  "constraints": ["нет политики", "нет шейминга"]
}
```

#### VoiceProfile (JSONB внутри ShowBible.voice_profiles)
```json
{
  "host_name": "Alex",
  "tts_provider": "elevenlabs",
  "voice_id": "pNInz6obpgDQGcFmaJgB",
  "settings": {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.3,
    "speed": 1.0
  }
}
```

### Source
```
id: UUID (PK)
project_id: UUID (FK → Project)
source_type: Enum(PDF, WEB, VIDEO, AUDIO)
original_url: String?
file_path: String?  (MinIO key)
title: String?
published_at: DateTime?
domain_reliability: Enum(HIGH, MEDIUM, LOW, UNKNOWN)
ingestion_status: Enum(PENDING, ENQUEUED, PROCESSING, COMPLETED, FAILED)
is_temporary: Boolean (default false)  # Fast web evidence — snippet only, not fully ingested
fully_ingested: Boolean (default false)  # True after deep ingestion pipeline completes
raw_text_path: String?  (MinIO key — full extracted text stored in object store, NOT in Postgres)
text_preview: String?  (first 500 chars for quick display)
metadata: JSONB  (author, page_count, duration, language, token_count, etc.)
is_duplicate_of: UUID? (FK → Source, for dedup flagging)
created_at: DateTime
```

**Правило TempSource:**
- Fast Web Evidence (Phase 3.2A) создаёт Source с `is_temporary=true, fully_ingested=false`
- Deep Ingestion (Phase 3.2B) при завершении → `is_temporary=false, fully_ingested=true`
- **Citation Auditor rule:** если claim опирается ТОЛЬКО на temporary sources → максимум OBSERVED/UNVERIFIED, обязательный хедж в речи ("по данным...", "в статье утверждается...")

**Жизненный цикл TempSource:**
- TTL: привязка к generation_run. После завершения run + grace period (7 дней) → soft delete
- Celery beat cron: `cleanup_expired_temp_sources` — раз в сутки
- **Temp sources исключены из основного retrieval:** Hunter и Act Planner фильтруют `is_temporary=false` по умолчанию. Temp evidence используется ТОЛЬКО внутри Fast Web Evidence шага текущего run
- Temp sources не участвуют в dedup (canonicalizer их игнорирует)

### Chunk
```
id: UUID (PK)
source_id: UUID (FK → Source)
parent_chunk_id: UUID? (FK → Chunk, self-ref)
content: Text
heading_path: String[]  (["Chapter 1", "Section 1.2"])
chunk_index: Int
token_count: Int
qdrant_point_id: UUID
```

### ContentMap (project-level aggregated scout output)
```
id: UUID (PK)
project_id: UUID (FK → Project)
map_json: JSONB  {topics, entities, key_terms, corpus_stats}
created_at: DateTime
```

### EvidenceSpan
```
id: UUID (PK)
project_id: UUID (FK → Project)
source_id: UUID (FK → Source)
chunk_id: UUID? (FK → Chunk)
verbatim_quote: Text  (обязательно — на языке оригинала источника)
locator: JSONB  {type: "page"|"section"|"timestamp"|"css_selector", value: ...}
start_idx: Int?
end_idx: Int?
reliability_hint: String
asset_type: Enum(STATISTIC, ANECDOTE, EXPERT_QUOTE, DEFINITION, MECHANISM, CASE, TIMELINE, CONTRAST)
narrative_value_score: Float (0-1)
tellability_notes: String?
created_at: DateTime
```

### Claim
```
id: UUID (PK)
project_id: UUID (FK → Project)
claim_type: Enum(FACT, OPINION, REPORT)
verification_level: Enum(OBSERVED, VERIFIED, DISPUTED, UNVERIFIED)
statement: Text  (на целевом языке подкаста)
attribution: JSONB?  {who, where, when}  (обязательно для OPINION/REPORT)
as_of_date: Date?
notes: Text?
created_at: DateTime
updated_at: DateTime
```

### ClaimEvidence (many-to-many)
```
claim_id: UUID (FK → Claim)
evidence_id: UUID (FK → EvidenceSpan)
relation: Enum(SUPPORTING, COUNTER, ILLUSTRATIVE)
```

### ConflictBrief
```
id: UUID (PK)
project_id: UUID (FK → Project)
claim_ids: UUID[]
what_conflicts: Text
why_reason: Text
how_to_present: Text
```

### GenerationRun
```
id: UUID (PK)
project_id: UUID (FK → Project)
user_id: UUID (FK → User)
show_bible_id: UUID (FK → ShowBible)
status: Enum(QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED)
config: JSONB  {duration_target, language, user_prompt}
current_phase: String?
current_step: String?
progress_pct: Float?
langgraph_thread_id: String?  (для checkpointing/resume)
estimated_cost: JSONB?  {llm_cost, tts_cost, total}
actual_cost: JSONB?  {llm_cost, tts_cost, total, token_counts}
error_message: Text?
started_at: DateTime?
completed_at: DateTime?
created_at: DateTime
```

### EpisodeManifest
```
id: UUID (PK)
generation_run_id: UUID (FK → GenerationRun)
research_tree_json: JSONB
duration_fill_layers: String[]
act_count: Int (2-6)
acts_json: JSONB [Act]  — см. ниже
```

#### Act (JSONB внутри acts_json)
```json
{
  "act_number": 1,
  "title": "Introduction & Definitions",
  "estimated_duration_seconds": 600,
  "claim_ids": ["c1", "c2", "c5"],
  "conflict_brief_ids": [],
  "content_quotas": {
    "verified_facts": 3,
    "attributed_opinions": 1,
    "narrative_assets": 1,
    "conflicts": 0,
    "takeaways": 1
  },
  "beat_sheet": [
    {
      "scene": 1,
      "type": "hook",
      "description": "Provocative question",
      "claim_ids": ["c1"],
      "evidence_ids": ["e5"],
      "estimated_duration_seconds": 45,
      "transition_note": null,
      "notes": "Host A asks, Host B responds"
    }
  ]
}
```

### ScriptSegment (сценарий хранится в БД, НЕ в state — state-by-IDs!)
```
id: UUID (PK)
generation_run_id: UUID (FK → GenerationRun)
act_number: Int
scene_number: Int
dialogue_json: JSONB [
    {
      "speaker": "A",
      "text": "Знаешь, что самое удивительное...",
      "ssml_hints": {"break_after": "500ms", "emphasis": [...]},
      "emotion": "curious",
      "claim_ids": ["c1"]
    }
]
engagement_score: Float?  (0-1, from Writer Revolt eval)
audit_status: Enum(PENDING, PASSED, FAILED, REVISED)
revision_count: Int (default 0)

# Per-segment quality metrics (заполняются Citation Auditor)
quality_metrics: JSONB {
    fact_coverage_ratio: Float,           # доля factual lines с claim_id привязкой
    forbidden_phrase_hits: Int,            # кол-во нарушений forbidden_phrases
    rigor_policy_violations: Int,          # UNVERIFIED сказан как факт, etc.
    persona_consistency_score: Float,      # 0-1, LLM-critique: соответствие HostPersona
    temp_source_only_claims: Int           # claims опирающиеся только на temporary sources
}

created_at: DateTime
updated_at: DateTime
```

**Зачем per-segment metrics:**
- Debugging: видно где именно "потекло" качество
- UI: подсветка проблемных сегментов в интерфейсе
- Partial regeneration: возможность перегенерировать только плохие сегменты

### Episode
```
id: UUID (PK)
generation_run_id: UUID (FK → GenerationRun, unique)
project_id: UUID (FK → Project)
title: String
description: Text?
language: Enum(EN, RU)
duration_seconds: Int
audio_file_path: String  (MinIO key)
transcript_json: JSONB  (timestamped dialogue: [{start_ms, end_ms, speaker, text}])
shownotes_json: JSONB  (structured references)
quality_metrics: JSONB  {
    claim_coverage_pct: Float,
    verified_fact_pct: Float,
    disputed_addressed_count: Int,
    narrative_assets_per_section: Float,
    audience_level_match: Float
}
created_at: DateTime
```

---

## 4. LangGraph State Definition

**Строго по правилу State-by-IDs: никаких текстов, объектов, диалогов в state.**

```python
class PipelineState(TypedDict):
    # Identity
    generation_run_id: str
    project_id: str
    user_id: str

    # Config (compact, immutable)
    user_prompt: str
    duration_target: int          # seconds
    language: str                 # "en" | "ru"

    # Phase 0 output
    show_bible_id: str

    # Phase 1 outputs (pre-populated before graph starts)
    source_ids: list[str]
    content_map_id: str           # FK → ContentMap

    # Phase 2 outputs
    manifest_id: str              # holds research_tree + duration_fill_layers
    # (research_tree и duration_fill_layers хранятся в EpisodeManifest)

    # Phase 3 outputs
    evidence_ids: list[str]
    pending_deep_ingestion_job_ids: list[str]  # Celery task IDs

    # Phase 4 outputs
    claim_ids: list[str]
    conflict_brief_ids: list[str]

    # Phase 5 outputs
    # manifest_id уже в state — acts добавляются в EpisodeManifest.acts_json

    # Phase 6 outputs
    script_segment_ids: list[str]  # FK → ScriptSegment (НЕ тексты!)

    # Phase 7 outputs
    episode_id: str

    # Control flow
    current_phase: str
    iteration_counts: dict        # {writer_planner_loops: int, fast_web_per_act: int, deep_ingestion_active: int}
    quality_signals: dict         # обновляется после Citation Auditor
```

**Важно:** Ни одна нода не получает state целиком. Каждая нода:
1. Берёт нужные *_id из state
2. Загружает данные из PostgreSQL/Qdrant по этим ID (lazy loading)
3. Записывает результат в БД
4. Возвращает только новые *_id в state

---

## 5. Детальное описание каждой фазы

### Фаза 0: Show Setup — "The Show Bible"

**LangGraph Node: `setup_show`**

**Вход:**
- `user_prompt` — тема подкаста
- `duration_target`
- Пользовательские настройки: audience_level, show_style, tone_preference (если заданы)
- Или ID пресета ShowBible

**LLM: Claude Sonnet 4**

**Процесс:**
- Если пользователь выбрал пресет → загрузить, адаптировать под тему
- Если custom → Show Configurator Agent генерирует:
  - audience_profile: что аудитория знает, что объяснять
  - 2 HostPersona с дополняющими ролями (Guide + Challenger, или Narrator + Skeptic, etc.)
  - tone_and_mood, show_style под тему
  - forbidden_phrases (LLM-клише под стиль)
  - rigor_policy (уровень строгости фактчека)
  - adjacency_policy (правила "ухода в сторону")
  - explanation_policy (как объяснять сложное для данной аудитории)
  - format_rules (длина реплик, частота резюме, юмор)
- Ошибка генерации ShowBible (LLM call fail / invalid JSON / invalid payload shape) не маскируется fallback-конфигом:
  - выбрасывается `ShowBibleGenerationError`
  - run/API получает явную ошибку
- VoiceProfile — пользователь выбирает голоса в UI при настройке генерации:
  - Хранятся в ShowBible.voice_profiles (привязка host_name → voice_id)
  - Для run-scoped ShowBible: voice выбор сохраняется в копии
  - Дефолт: если голоса не выбраны → используются первые 2 из ElevenLabs voices list (мужской + женский)

**Выход:**
- `ShowBible` record в PostgreSQL
- `show_bible_id` → в state

**Пример ShowBible:**
```json
{
  "audience_profile": {
    "level": "casual",
    "knows": ["что такое биткоин на базовом уровне"],
    "explain": ["хеширование", "proof of work", "ноды", "форк"]
  },
  "tone_and_mood": "investigative",
  "show_style": "Investigative",
  "hosts": [
    {
      "name": "Ведущий",
      "role": "Guide",
      "vocabulary_rules": "простые слова, технические термины только с объяснением",
      "interaction_style": "ведёт нарратив, структурирует, подводит итоги",
      "explanation_behavior": "аналогии из повседневной жизни",
      "constraints": ["нет финансовых советов"]
    },
    {
      "name": "Аналитик",
      "role": "Challenger",
      "vocabulary_rules": "может использовать профессиональный жаргон, но раскрывает",
      "interaction_style": "задаёт неудобные вопросы, требует доказательств",
      "explanation_behavior": "через цифры и сравнения",
      "constraints": ["нет финансовых советов"]
    }
  ],
  "forbidden_phrases": [
    "В заключение хочу сказать",
    "Как известно",
    "Давайте разберёмся",
    "Итак, друзья",
    "Без лишних слов"
  ],
  "rigor_policy": "Ключевые FACT в основной линии должны быть VERIFIED. Для edge cases допустим OBSERVED.",
  "adjacency_policy": "Adjacent expansion только если: (а) усиливает понимание core механизма, (б) иллюстрирует конфликт/кейс, (в) reader может не знать контекст. Никаких general-purpose tangents.",
  "explanation_policy": "Каждый новый термин: определение → аналогия → пример. Числа: интуитивный перевод (\"это как если бы...\").",
  "format_rules": {
    "max_reply_tokens": 150,
    "summary_every_n_scenes": 3,
    "humor_level": "light",
    "profanity_allowed": false
  }
}
```

**Format_rules presets по show_style:**
| show_style | max_reply_tokens | summary_every_n | humor_level | типичная структура |
|---|---|---|---|---|
| Investigative | 120 | 4 | minimal | длинные расследования, cliffhanger'ы |
| Conversational | 100 | 3 | light-medium | короткие реплики, естественный диалог |
| Academic Debate | 200 | 5 | minimal | длинные аргументы, formal tone |
| Explainer | 80 | 2 | light | bite-size объяснения, частые резюме |

```text (для ясности — выше уже закрыта JSON-секция)
```

---

### Фаза 1: Ingestion & Indexing

> Выполняется ДО LangGraph — при загрузке источников пользователем. Каждый Source обрабатывается через Celery task.

**Runtime (Celery worker):**
- Жизненный цикл статуса ingestion: `PENDING -> ENQUEUED -> PROCESSING -> COMPLETED|FAILED`.
- Транзиентные SQLAlchemy/DB ошибки в ingestion task ретраятся; при исчерпании ретраев source переводится в `FAILED` с текстом ошибки.
- После успешного ingestion устаревшее `metadata.error` очищается.

#### 1.1 Cascade Ingestion

**Вход:** `Source` record (source_type, file_path/original_url)

**Процесс:**
- **PDF:** MinIO → `marker` → Markdown (H1/H2/списки/таблицы сохранены)
- **Web:** URL → Jina Reader API → если fail → FireCrawl → если fail → Playwright headless + readability
- **Video/Audio:** файл/URL → Deepgram SDK (nova-3, smart_format=True, diarize=True, paragraphs=True, language="auto")

**Выход:**
- Full text → MinIO → `Source.raw_text_path` (тексты не в Postgres — PDF/стенограммы могут быть огромными)
- `Source.text_preview` — первые 500 chars
- `Source.metadata` {author, page_count, duration, language, token_count}
- `Source.ingestion_status` = COMPLETED

#### 1.1b Text Normalization

**Модуль:** `app/ingestion/normalize.py`

**Вход:** raw extracted text (после сохранения в MinIO — оригинал сохранён)

**Процесс:**
- **Universal (все source types):**
  - Unicode NFC normalization (предотвращает invisible mismatches при retrieval)
  - Whitespace normalization: NBSP→пробел, collapse множественных пробелов/табов, 3+ newlines→2, strip trailing spaces
- **PDF-only:**
  - Hyphenation repair: `inter-\nnational` → `international` (line-break hyphens из PDF layout)
  - Page number stripping: standalone числа на отдельных строках (`42`, `— 3 —`)
  - Boilerplate removal: строки повторяющиеся >3 раз и <120 chars (headers, footers, copyright)

**Выход:** нормализованный текст → передаётся в chunker. `Source.text_preview` тоже берётся из нормализованного текста.

**Важно:** raw text в MinIO (`Source.raw_text_path`) остаётся нетронутым — audit trail.

#### 1.2 Structural Chunking

**Стратегия: structure-first, then normalize.**

**Вход:** нормализованный текст (после шага 1.1b)

**Процесс:**
1. **Первый проход — структурное разбиение:**
   - Режем по H2/H3 заголовкам и семантическим разделителям (--- , пустые строки ≥ 2)
   - Каждый структурный блок получает heading_path (хлебные крошки заголовков)
   - Сохраняем parent-child: H1 → H2 → H3

2. **Второй проход — нормализация размера:**
   - Блоки > 1024 tokens → делим внутри секции по абзацам, НЕ ломая heading_path
   - Блоки < 128 tokens → объединяем с соседним блоком той же секции
   - **Правило:** никогда не резать между заголовком и первым абзацем под ним
   - Финальный overlap: 64 tokens между соседними чанками одного уровня

3. **Результат:** чанки 256-1024 tokens, с сохранённой иерархией
   - Qdrant payload: {source_id, chunk_id, heading_path: ["H1 > H2 > H3"], source_type, domain_reliability}

**Выход:**
- `Chunk` records в PostgreSQL
- Embeddings в Qdrant (text-embedding-3-large, dim=3072)
  - Payload: {source_id, chunk_id, heading_path, source_type, domain_reliability}

#### 1.3 Canonicalization & Metadata

**Вход:** все Sources в проекте

**Процесс (LLM: Haiku для classification):**
- Дедупликация: URL canonical + title cosine similarity (embedding) + content SimHash
  - Дубликат → `Source.is_duplicate_of` = original_id (не удаляем)
- published_at: regex паттерны → если нет, Haiku извлекает из текста
- domain_reliability:
  - journal/arxiv/gov → HIGH
  - major news (NYT, BBC, Reuters, etc.) → MEDIUM
  - established tech blog → MEDIUM
  - personal blog/unknown → LOW
  - no domain info → UNKNOWN

**Выход:** `Source.published_at`, `Source.domain_reliability`, `Source.is_duplicate_of`

#### 1.4 Scouting (Content Map)

**Вход:** все Chunks проекта (heading_path + first 200 chars each)

**LLM: Haiku** (structured output)

**Процесс:** Агрегация тем, сущностей, терминов. Компрессия до 3-5k tokens.

**Выход → ContentMap record:**
```json
{
  "topics": [
    {"heading": "История Bitcoin", "subtopics": ["Satoshi", "Whitepaper", "Genesis Block"], "source_ids": ["s1", "s2"]}
  ],
  "entities": [
    {"name": "Satoshi Nakamoto", "type": "person", "frequency": 12},
    {"name": "Proof of Work", "type": "concept", "frequency": 8}
  ],
  "key_terms": ["blockchain", "mining", "hash rate", "fork"],
  "corpus_stats": {"total_sources": 5, "total_chunks": 142, "languages": ["en", "ru"]}
}
```

---

### Фаза 2: Query Decomposition → Research Tree

**LangGraph Node: `decompose`**

#### 2.1 Decomposer Agent

**Вход:**
- `user_prompt`
- `duration_target`
- ContentMap (загрузка по content_map_id)
- ShowBible (загрузка по show_bible_id) → audience_profile, adjacency_policy

**LLM: Claude Sonnet 4** (structured output)

**Процесс:**
- Анализирует content_map: что уже покрыто корпусом, где дыры
- Строит Research Tree с учётом:
  - audience_profile: что нужно объяснять, какие термины определять
  - adjacency_policy: ограничения на уход в сторону
  - duration_target: сколько material нужно
- Для каждой ветки: intent, expected_evidence_type, quality_need, explanation_hints

**Выход — Research Tree (5-7 angles, 3-5 drill-downs/angle):**
```json
{
  "root_question": "Полная история Bitcoin: от идеи до мировой финансовой системы",
  "branches": [
    {
      "id": "b1",
      "question": "Как и почему появился Bitcoin?",
      "intent": "history",
      "expected_evidence_type": ["TIMELINE", "CASE", "EXPERT_QUOTE"],
      "quality_need": "VERIFIED",
      "priority": 1,
      "explanation_hints": ["объяснить cypherpunk movement для casual аудитории"],
      "children": [
        {
          "id": "b1.1",
          "question": "Кто такой Satoshi Nakamoto и почему анонимность?",
          "intent": "explain",
          "expected_evidence_type": ["ANECDOTE", "EXPERT_QUOTE"],
          "quality_need": "OBSERVED",
          "priority": 1,
          "explanation_hints": []
        }
      ]
    }
  ]
}
```

#### 2.2 Duration Fill Strategy

**Часть того же LLM call.**

**Алгоритм выбора слоёв:**
1. Оценить: сколько минут покрывает core material (branches × est. speech rate)
2. Если core ≥ duration_target → только layers 1-2
3. Если core < duration_target → активировать слои по порядку, пока не покроем:
   - L1: Definitions & scope (~3 мин/тему)
   - L2: Core mechanisms (~5 мин/механизм)
   - L3: History/evolution (~5-10 мин)
   - L4: Case studies × 2-4 (~3-5 мин/кейс)
   - L5: Edge cases / failure modes (~3-5 мин)
   - L6: Debates & contradictions (~5-8 мин/спор)
   - L7: Adjacent expansion (~3-5 мин/тема) **← guardrail: строго по adjacency_policy**
   - L8: Practical takeaways (~2-3 мин)

**Guardrail для Adjacent Expansion (L7):**
- Каждая смежная тема ДОЛЖНА быть обоснована: "усиливает понимание X" / "иллюстрирует конфликт Y" / "необходимый контекст для Z"
- Максимум 2 adjacent topics для подкаста ≤ 30 мин, 4 для ≤ 60 мин, 6 для 90+ мин
- Если audience_level = professional → adjacent expansion минимальный

**Выход:**
- research_tree + duration_fill_layers → сохраняются в `EpisodeManifest`
- `manifest_id` → в state

---

### Фаза 3: Targeted Research (Closed-book + Open-web, двухскоростной)

**Явное разделение двух скоростей:**

#### 3.1 Hunter Agent (Closed-book retrieval)

**LangGraph Node: `hunt_closed_book`**

**Вход:**
- research_tree (загрузка из EpisodeManifest по manifest_id)
- source_ids

**Процесс:**
- Для каждой ветки research tree:
  1. Vector search в Qdrant (query = branch.question, top_k=10, filter: source_id ∈ project sources)
  2. PostgreSQL full-text search по chunks (tsvector)
  3. Hybrid merge: RRF (Reciprocal Rank Fusion) из обоих результатов
  4. LLM (Haiku): из top-5 chunks извлечь EvidenceSpan:
     - verbatim_quote (exact quote from source)
     - locator (page/section/timestamp)
     - asset_type classification
     - narrative_value_score (0-1)
     - tellability_notes

**Tools (LangGraph ToolNode):**
- `vector_search(query: str, top_k: int, source_ids: list[str]) → list[{chunk_id, score, preview}]`
- `keyword_search(query: str, source_ids: list[str]) → list[{chunk_id, score, preview}]`
- `get_chunk_content(chunk_id: str) → {content, heading_path, source_metadata}`

**Выход:**
- Новые EvidenceSpan records → evidence_ids в state
- Gap report: ветки research tree где evidence недостаточно

#### 3.2A Librarian Fast (Open-web, non-blocking, в цикле)

**LangGraph Node: `research_fast_web`**

**Вход:**
- Gap report от Hunter (ветки с недостаточным evidence)
- evidence_ids (чтобы не дублировать)
- ShowBible.rigor_policy (для понимания приемлемого quality_need)

**LLM: Claude Sonnet 4** (с tool-calling)

**Tools:**
- `web_search(query: str) → list[{title, url, snippet, score}]` (Tavily API)
- `fetch_page(url: str) → {title, content_markdown, published_at?}` (Jina Reader)
- `create_web_evidence(url: str, quote: str, asset_type: str, ...) → evidence_id` (создаёт Source(type=WEB) + EvidenceSpan)

**Ограничения (governors):**
- Макс **5 web-источников на акт** (tracked в iteration_counts.fast_web_per_act)
- Penalize reliability < MEDIUM: web evidence с LOW domain → только OBSERVED, никогда VERIFIED
- FACT из web → OBSERVED only ("в статье X сказано...")
- OPINION/REPORT → допускается с 1 web-источником + обязательная атрибуция

**Важно: Act Planner не может ждать deep ingestion. Только fast evidence.**

**Security guardrails для web ingestion:**
- **SSRF protection:** denylist локальных IP (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.169.254), cloud metadata endpoints
- **File size limit:** максимум 50MB для скачиваемых файлов
- **Domain rules:** configurable allow/deny lists, penalize domains из LOW reliability
- **Timeout:** 30s на fetch, 120s на full ingestion
- **Content validation:** проверка MIME type, отклонение бинарных файлов (кроме PDF)
- **Sandbox:** парсеры (Marker, Playwright) запускаются в изолированных Celery worker'ах с ограниченными ресурсами

**Выход:**
- Новые Source (is_temporary=true) + EvidenceSpan records
- evidence_ids обновлены в state

#### 3.2B Deep Web Ingestion (Celery async, улучшитель качества)

**Celery Task (НЕ LangGraph node — полностью async)**

**Вход:**
- URLs от Librarian Fast, которые стоит полностью проиндексировать (длинные статьи, PDF, research papers)

**Процесс:**
1. Download content
2. Marker/Jina → full text extraction
3. Structural chunking (same as Phase 1.2)
4. Embeddings → Qdrant
5. По завершении:
   - Source.ingestion_status = COMPLETED
   - Поиск existing claims, которые можно **апгрейдить**: OBSERVED → VERIFIED (если deep source подтверждает)
   - Добавление точных locators/цитат к existing evidence
   - Redis pub/sub event → WebSocket

**Правила:**
- **Никогда не блокирует** основной pipeline
- Макс concurrent deep ingestion jobs: 3 (governor)
- Если pipeline уже на Phase 6+ когда deep ingestion завершается → benefit на текущий run минимален, но данные обогащают корпус для будущих runs

**Выход:**
- pending_deep_ingestion_job_ids в state (Celery task IDs)
- Verification upgrades happen async (Claims обновляются напрямую в БД)

---

### Фаза 4: Claim Building & Fact-Checking

#### 4.1 Claim Builder

**LangGraph Node: `build_claims`**

**Вход:**
- evidence_ids → загрузка EvidenceSpans из PostgreSQL
  - Lazy loading: verbatim_quote + asset_type + source metadata + reliability_hint
  - Группируем по source_id для context

**LLM: Claude Sonnet 4** (structured output)

**Процесс:**
- Кластеризация evidence по тематическим группам
- Для каждого кластера → Claim:
  - claim_type: FACT (проверяемое утверждение) | OPINION (чьё-то мнение) | REPORT (пересказ)
  - statement на **целевом языке подкаста** (но verbatim_quote в evidence остаётся на языке оригинала)
  - supporting_evidence_ids
  - illustrative_evidence_ids: ANECDOTE/CASE/EXPERT_QUOTE для narrative value
  - attribution: для OPINION/REPORT → {who, where, when} — обязательно!
- НЕ стерилизует: narrative assets сохранены в illustrative_evidence_ids

**Выход:**
- Claim + ClaimEvidence records → claim_ids в state

#### 4.2 Verification / Triangulation

**LangGraph Node: `verify_claims`**

**Вход:**
- claim_ids → загрузка Claims + supporting evidence metadata

**LLM: Claude Sonnet 4**

**Алгоритм верификации FACT claims:**
```
if supporting_evidence ≥ 2 from independent sources:
    → VERIFIED
elif supporting_evidence = 1 from HIGH reliability source:
    → VERIFIED
elif supporting_evidence = 1 from MEDIUM reliability source:
    → OBSERVED
elif supporting_evidence = 1 from LOW/UNKNOWN source:
    → UNVERIFIED
elif counter_evidence exists:
    → DISPUTED (формируем counter_evidence_ids)
else:
    → UNVERIFIED
```

**Для OPINION/REPORT:**
- attribution {who, where, when} заполнена? → OK
- Нет → request_additional_research("Find attribution for: ...")

**Tools:**
- `update_claim(claim_id, verification_level, counter_evidence_ids?)` → PostgreSQL
- `request_research(question, context_claim_id)` → добавляет ветку → loop back to Phase 3 (**макс 2 итерации**)

**Выход:**
- Claims обновлены с verification_level
- Возможно: re-entry в Phase 3 (макс 2 раза)

#### 4.3 Contradiction Resolver

**LangGraph Node: `resolve_conflicts`**

**Вход:**
- Claims с verification_level = DISPUTED
- Их supporting + counter evidence (lazy loaded)

**LLM: Claude Sonnet 4**

**Выход → ConflictBrief:**
```json
{
  "claim_ids": ["c5", "c12"],
  "what_conflicts": "Source A says Bitcoin uses 0.5% of world energy, Source B says 0.1%",
  "why_reason": "Different measurement methodologies: A uses Cambridge estimate, B uses mining pool self-reports",
  "how_to_present": "Acknowledge range, explain methodological difference, let listener decide"
}
```
→ conflict_brief_ids в state

---

### Фаза 5: Hierarchical Planning

#### 5.1 Showrunner

**LangGraph Node: `plan_episode`**

**Вход:**
- duration_target
- ShowBible (по show_bible_id): show_style, rigor_policy, format_rules
- claim_ids → загрузка Claims (statement + type + verification_level)
- conflict_brief_ids → загрузка ConflictBriefs
- duration_fill_layers (из EpisodeManifest)

**LLM: Claude Sonnet 4**

**Процесс:**
- Определение количества актов (2-6) на основе:
  - duration_target
  - show_style (investigative → больше актов, conversational → меньше)
  - плотность claims
- Распределение claims по актам:
  - Каждый claim — ровно в 1 акте (anti-duplication)
  - ConflictBriefs → в акт где основной claim
- Content quotas per act:
  - Мин. verified_facts (из rigor_policy)
  - attributed_opinions
  - Мин. 1 narrative asset (ANECDOTE/CASE/QUOTE)
  - conflicts (из ConflictBriefs)
  - 1 takeaway
- Если claims недостаточно для duration → активировать следующий duration_fill_layer

**Quality gate — DISPUTED claims:**
- Если claim DISPUTED в основной линии → Showrunner **обязан** выделить слот "debate moment" в соответствующей секции
- Constraint: каждый DISPUTED claim из основной линии должен быть assigned к акту с content_quota.conflicts ≥ 1
- Если DISPUTED claim не получил слот → manifest validation fails → Showrunner retry

**Выход:** Episode Manifest обновлён с acts_json

#### 5.2 Act Planners (параллельные — LangGraph Send())

**LangGraph Node: `plan_act` (fan-out per act)**

**Вход (per act):**
- Act из manifest (claim_ids, content_quotas, conflict_brief_ids)
- Claims + top illustrative evidence (lazy loaded, **только для этого акта**)
- ShowBible: hosts, format_rules, explanation_policy

**LLM: Claude Sonnet 4**

**Процесс:**
- Beat Sheet: список сцен с estimated duration
- Сцены типизированы: hook, explanation, debate, case_study, transition, summary, takeaway
- Каждая сцена привязана к claim_ids + evidence_ids
- Если quotas не заполнены:
  - `search_evidence(query, asset_type)` → ищет в existing evidence
  - `request_fast_research(question)` → Phase 3.2A (fast only!)
  - **НЕ может ждать deep ingestion** — использует только fast evidence

**Координация параллельных Act Planners:**
- Redis lock на web search queries (TTL=60s): если Act Planner A ищет "Bitcoin energy consumption", Act Planner B использует cached result
- Каждый Act Planner видит **только свои claim_ids** (no overlap)

**Tools:**
- `search_evidence(query, asset_type?, source_ids?) → list[evidence]`
- `request_fast_research(question) → evidence_ids`

**Выход:**
- Beat sheets → обновление acts_json в EpisodeManifest

#### 5.3 Integrator

**LangGraph Node: `integrate_acts`**

**Вход:**
- Все beat sheets (из EpisodeManifest.acts_json)
- Claims involved

**LLM: Claude Sonnet 4**

**Процесс:**
- Проверка стыков между актами → transition_note для каждого стыка
- Анти-повторы: claim в >1 beat sheet? → убрать из позднего или дать callback ("как мы обсуждали ранее")
- Нарастание сложности: Act 1 = base terms → Act N = nuanced debates
- Контроль темпа: чередование "плотных фактов" и "лёгких историй"

**Выход:**
- acts_json обновлены с transition_notes
- manifest_id остаётся в state

---

### Фаза 6: Writers Room

#### 6.1 Screenwriter

**LangGraph Node: `write_screenplay` (per act, sequential)**

**Вход:**
- Beat sheet данного акта (из EpisodeManifest)
- Claims + evidence (lazy loaded: supporting + illustrative для этого акта)
- ConflictBriefs для этого акта
- ShowBible: hosts (persona, role, vocabulary), forbidden_phrases, format_rules, explanation_policy
- language

**LLM: Claude Sonnet 4**

**Процесс:**
- Для каждой сцены из beat sheet → диалог:
  - Каждая реплика: speaker, text, ssml_hints, emotion, claim_ids
  - Строго следует HostPersona roles:
    - Guide: ведёт нарратив, объясняет, резюмирует
    - Challenger: сомневается, требует доказательств, задаёт неудобные вопросы
  - Правила речи по verification_level (из PIPELINE.md):
    - VERIFIED → уверенно
    - OBSERVED/UNVERIFIED → хедж ("по данным...", "в отчёте...")
    - DISPUTED → "тут интересный спор..." + обе стороны
    - OPINION/REPORT → атрибуция ("Karpathy считает...")
  - Использование illustrative evidence: анекдоты, кейсы, метафоры, цитаты
  - explanation_policy: термин → определение → аналогия → пример (если audience = casual/beginner)
  - forbidden_phrases: каждая реплика проверяется и НЕ содержит
  - format_rules: max_reply_tokens, summary каждые N сцен

**Выход:**
- `ScriptSegment` records в PostgreSQL (по сценам)
- script_segment_ids в state

#### 6.2 Writer Revolt Loop

**LangGraph Node: `evaluate_and_revolt`**

**Вход:**
- script_segment_ids → загрузка ScriptSegments
- ShowBible (для persona check)

**LLM: Haiku для оценки, Sonnet для переписывания**

**Оценка engagement (Haiku):**
```json
{
  "score": 0.45,
  "weak_points": [
    {"scene": 3, "issue": "dry_facts", "suggestion": "needs ANECDOTE or CASE study"},
    {"scene": 5, "issue": "persona_mismatch", "suggestion": "Challenger accepts without pushback"}
  ]
}
```

**Легитимные причины revolt (из PIPELINE.md):**
- "Сцена скучная — нужен CASE/ANECDOTE/CONTRAST"
- **Persona mismatch:**
  - "Challenger не поверит без stronger VERIFIED пруфа"
  - "Нужен контраргумент для честного спора"

**Допустимые запросы revolt'а (ровно 3 типа):**
1. `request_stronger_evidence(claim_id)` → ищет VERIFIED evidence
2. `request_counter_argument(claim_id)` → ищет counter evidence для дебата
3. `request_illustrative_asset(claim_id, asset_type)` → ищет ANECDOTE/CASE/CONTRAST

**Запрещено:** "Уберём факт потому что не в характере" — персона НЕ переписывает истину, только просит лучшую подачу

**Лимиты revolt:**
- Макс 2 engagement revolts на сцену (boring → rewrite)
- Макс 1 persona-mismatch revolt на сцену (stronger proof / counter-argument)
- Суммарно на акт: макс 4 revolt'а (governor: iteration_counts.writer_planner_loops)

**Процесс:**
- score < 0.6 → revolt:
  1. Запрос в Act Planner: "нужен ANECDOTE/CASE/CONTRAST для claim X"
  2. Act Planner ищет evidence или triggers fast research
  3. Screenwriter переписывает weak scenes
- Макс **2 итерации на сцену** (governor: iteration_counts.writer_planner_loops)

**Выход:**
- ScriptSegments обновлены (revision_count++)
- engagement_score записан

#### 6.3 Citation Auditor

**LangGraph Node: `audit_citations`**

**Вход:**
- script_segment_ids → загрузка ScriptSegments (dialogue_json)
- claim_ids → загрузка Claims
- ShowBible → rigor_policy, forbidden_phrases

**LLM: Claude Sonnet 4**

**Проверки:**
1. Каждый factual statement → привязан к claim_id? Если нет → FLAG
2. Формулировка ↔ verification_level:
   - UNVERIFIED утверждается уверенно? → FAIL
   - DISPUTED без упоминания обеих сторон? → FAIL
   - OPINION/REPORT без атрибуции? → FAIL
3. rigor_policy: "ключевые FACT должны быть VERIFIED" → проверить
4. forbidden_phrases: ни одна реплика не содержит запрещённые фразы
5. audience_level: термины объяснены (для casual/beginner) / не пережёваны (для professional)

**Выход:**
```json
{
  "passed": false,
  "issues": [
    {"segment_id": "...", "line": 5, "type": "unverified_stated_as_fact", "suggestion": "Add hedge: 'по некоторым данным...'"},
    {"segment_id": "...", "line": 12, "type": "forbidden_phrase", "text": "Как известно", "suggestion": "Remove or rephrase"}
  ]
}
```
- Если not passed → Screenwriter fix flagged lines (**макс 1 iteration**)
- Quality signals обновлены:
```json
{
  "claim_coverage_pct": 94.5,
  "verified_fact_pct": 78.0,
  "disputed_addressed_count": 2,
  "narrative_assets_per_section": 1.5,
  "audience_level_match": 0.9
}
```

---

### Фаза 7: Audio & Shownotes

#### 7.1 TTS Generation

**LangGraph Node: `generate_audio`**

**Вход:**
- script_segment_ids → загрузка ScriptSegments (dialogue_json)
- ShowBible → voice_profiles (provider, voice_id, settings per host)
- language

**Процесс:**
1. SSML preprocessing: конвертация ssml_hints в ElevenLabs subset:
   - `break_after` → `<break time="Xms"/>`
   - `emphasis` → strength mapping
   - Остальное (тон, темп) → через ElevenLabs voice settings per-request
2. Для каждой реплики → ElevenLabs API call:
   - Voice ID из VoiceProfile для speaker
   - Settings: stability, similarity_boost, style, speed
   - Результат: MP3 audio segment → MinIO
3. Параллельная генерация: batch по 5-10 реплик (rate limit aware)
4. Post-processing (pydub + ffmpeg):
   - Конкатенация сегментов
   - Паузы между репликами: 300-800ms based on emotion transitions
   - Loudness normalization: LUFS -16 (podcast standard)
   - Fade in (2s), fade out (3s)
   - Export: MP3 320kbps

**Выход:**
- Аудио → MinIO → `Episode.audio_file_path`
- `Episode.duration_seconds`
- `Episode.transcript_json`:
```json
[
  {"start_ms": 0, "end_ms": 3200, "speaker": "A", "text": "Знаешь, что самое удивительное..."},
  {"start_ms": 3500, "end_ms": 7100, "speaker": "B", "text": "По данным на 2024 год..."}
]
```

#### 7.2 Shownotes Generation

**LangGraph Node: `build_shownotes`**

**Вход:**
- script_segment_ids (→ claim_ids из dialogue)
- Claims → Evidence → Sources (lazy loaded chain)
- ConflictBriefs

**LLM: Haiku** (structured output)

**Выход → Episode.shownotes_json:**
```json
{
  "sections": [
    {
      "title": "Act 1: Origins of Bitcoin",
      "references": [
        {
          "claim": "Bitcoin was created in 2009 by pseudonymous Satoshi Nakamoto",
          "verification": "VERIFIED",
          "sources": [
            {"title": "Bitcoin: A Peer-to-Peer Electronic Cash System", "author": "Satoshi Nakamoto", "url": "https://bitcoin.org/bitcoin.pdf", "date": "2008-10-31", "quote": "A purely peer-to-peer version of electronic cash...", "locator": {"type": "page", "value": 1}}
          ]
        }
      ],
      "conflicts": [
        {
          "topic": "Bitcoin energy consumption estimates",
          "positions": ["Cambridge Centre: 0.5% of world electricity", "Mining pools self-report: 0.1%"],
          "reason": "Different measurement methodologies"
        }
      ]
    }
  ]
}
```

---

## 6. Агенты и их Tools (сводная таблица)

| Агент | LLM | Tools (tool calling) | Фаза |
|---|---|---|---|
| **Show Configurator** | Sonnet | — (structured output) | 0 |
| **Scout** | Haiku | — (structured output) | 1.4 |
| **Decomposer** | Sonnet | — (structured output) | 2 |
| **Hunter** | Haiku (extraction) + Sonnet (analysis) | `vector_search`, `keyword_search`, `get_chunk_content` | 3.1 |
| **Librarian Fast** | Sonnet | `web_search`, `fetch_page`, `create_web_evidence` | 3.2A |
| **Deep Ingester** | — (no LLM) | Celery task: download→parse→chunk→embed | 3.2B |
| **Claim Builder** | Sonnet | — (structured output) | 4.1 |
| **Verifier** | Sonnet | `update_claim`, `request_research` | 4.2 |
| **Conflict Resolver** | Sonnet | — (structured output) | 4.3 |
| **Showrunner** | Sonnet | — (structured output) | 5.1 |
| **Act Planner** | Sonnet | `search_evidence`, `request_fast_research` | 5.2 |
| **Integrator** | Sonnet | — (structured output) | 5.3 |
| **Screenwriter** | Sonnet | — (structured output per scene) | 6.1 |
| **Engagement Evaluator** | Haiku | — (score output) | 6.2 |
| **Citation Auditor** | Sonnet | — (audit output) | 6.3 |
| **Shownotes Generator** | Haiku | — (structured output) | 7.2 |

---

## 7. Real-time Events (WebSocket)

**Канал:** `ws://api/ws/generations/{generation_run_id}`

**Event types:**
```typescript
type WSEvent =
  | { type: "phase_started"; phase: string; description: string }
  | { type: "phase_completed"; phase: string; duration_ms: number }
  | { type: "step_started"; phase: string; step: string; detail?: string }
  | { type: "step_completed"; phase: string; step: string }
  | { type: "progress"; pct: number; message: string }
  | { type: "evidence_found"; count: number; asset_types: string[] }
  | { type: "claim_created"; claim_type: string; statement_preview: string }
  | { type: "claim_verified"; claim_id: string; level: string }
  | { type: "conflict_detected"; brief_preview: string }
  | { type: "act_planned"; act_number: number; scenes: number }
  | { type: "scene_written"; act: number; scene: number }
  | { type: "writer_revolt"; act: number; scene: number; reason: string }
  | { type: "audio_segment_generated"; act: number; scene: number }
  | { type: "deep_ingestion_completed"; source_title: string }
  | { type: "quality_update"; metrics: QualitySignals }
  | { type: "generation_completed"; episode_id: string }
  | { type: "generation_failed"; error: string }
```

**Механизм:** Pipeline nodes → Redis pub/sub → FastAPI WebSocket endpoint → client broadcast.

---

## 8. API Endpoints

### Auth
- `POST /api/auth/callback` — OAuth callback
- `GET /api/users/me` — текущий пользователь (JWT)

### Projects
- `POST /api/projects` — создать {title, description, language}
- `GET /api/projects` — список проектов пользователя
- `GET /api/projects/{id}` — детали + sources + show_bible
- `PATCH /api/projects/{id}` — обновить
- `DELETE /api/projects/{id}` — удалить (cascade)

### Show Bibles
- `GET /api/show-bibles/presets` — список пресетов
- `POST /api/projects/{id}/show-bible` — создать/обновить ShowBible {audience_level, show_style, tone, hosts, ...}
- `GET /api/projects/{id}/show-bible` — текущий ShowBible
- `POST /api/projects/{id}/show-bible/generate` — LLM-генерация ShowBible из {topic, audience_level, show_style}
  - при ошибке генерации/валидации возвращает `502` (без fallback-дефолтов)

### Sources
- `POST /api/projects/{id}/sources/upload` — upload файла (multipart, tus resumable)
- `POST /api/projects/{id}/sources/url` — добавить URL
- `GET /api/projects/{id}/sources` — список
- `DELETE /api/sources/{id}` — удалить
- `GET /api/sources/{id}/status` — статус ingestion

### Generations
- `POST /api/projects/{id}/estimate` — оценка стоимости {duration_target} → {estimated_llm_cost, estimated_tts_cost, estimated_total, estimated_minutes}
- `POST /api/projects/{id}/generate` — запуск {duration_target, user_prompt, show_bible_id?, show_bible_override?}
  - Если `show_bible_id` → используем как есть
  - Если `show_bible_override` (partial patch: audience_level, show_style, tone) → создаём run-scoped ShowBible (is_preset=false, linked to generation_run), не перезаписывая проектный
  - Если ни то ни другое → берём текущий ShowBible проекта
- `GET /api/generations/{id}` — статус
- `POST /api/generations/{id}/cancel` — отмена
- `POST /api/generations/{id}/resume` — resume после падения (checkpoint)
- `WS /api/ws/generations/{id}` — real-time events

### Episodes
- `GET /api/episodes` — список эпизодов пользователя
- `GET /api/episodes/{id}` — детали (metadata, quality metrics)
- `GET /api/episodes/{id}/audio` — stream MP3
- `GET /api/episodes/{id}/transcript` — транскрипт JSON
- `GET /api/episodes/{id}/shownotes` — shownotes JSON
- `DELETE /api/episodes/{id}` — удалить

### Voices
- `GET /api/voices` — доступные ElevenLabs voices (cached)

---

## 9. Frontend Pages

### Landing `/`
- Hero section: описание продукта
- CTA → Register/Login

### Dashboard `/projects`
- Список проектов (карточки: title, source count, episode count)
- Кнопка "New Project"

### Project Detail `/projects/[id]`
- **Sources tab:** список загруженных источников + статусы
  - Source uploader: drag-drop файлов + URL input
- **Show Bible tab:** редактор ShowBible
  - Выбор пресета или custom
  - Audience level selector (Beginner/Casual/Professional)
  - Show style selector (Investigative/Conversational/Academic Debate/Explainer)
  - Host persona editor (name, role, vocabulary, style)
  - Advanced: forbidden_phrases, rigor_policy, adjacency_policy
  - "Generate with AI" button → LLM создаёт ShowBible
- **Episodes tab:** список сгенерированных эпизодов
- Кнопка "Generate Podcast"

### Generation Config `/projects/[id]/generate`
- Form:
  - User prompt (textarea)
  - Duration target (slider: 15/30/45/60/90 мин)
  - Voice A / Voice B (select из ElevenLabs voices)
  - ShowBible summary (readonly, link to edit)
- **Cost estimate** panel (auto-calculated на основе duration + sources)
  - Estimated LLM cost
  - Estimated TTS cost
  - Estimated total
- "Start Generation" button

### Generation Progress `/projects/[id]/generate` (после запуска)
- Visual pipeline: 8 фаз (0-7) как stepper/timeline
  - Текущая подсвечена, предыдущие — зелёные
  - Внутри текущей — live steps
- Overall progress bar (%)
- Live feed:
  - "Show Bible configured: Investigative format, Guide + Challenger"
  - "Research tree: 6 angles, 18 drill-downs"
  - "Found 23 evidence spans in corpus"
  - "Verified claim: Bitcoin was created in 2009 ✓"
  - "Conflict: energy consumption estimates differ..."
  - "Writing Act 2, Scene 3..."
  - "Writer revolt: Scene 5 needs stronger case study"
  - "Citation audit passed ✓"
  - "Generating audio..."
- Можно уйти — генерация в фоне

### Episode Player `/episodes/[id]`
- Audio player с waveform (wavesurfer.js)
- Синхронизированный транскрипт (highlight текущей реплики при проигрывании)
- Shownotes: structured references, conflict blocks
- Quality metrics badge
- Download MP3
- Share link

---

## 10. План имплементации (порядок)

### Этап 1: Foundation
1. Init monorepo: `backend/` (uv + FastAPI), `frontend/` (Next.js + shadcn/ui)
2. Docker Compose: postgres, redis, qdrant, minio, api, worker, frontend
3. Database models + Alembic migrations (User, Project, Source, ShowBible, Chunk, etc.)
4. FastAPI app factory + Pydantic Settings config
5. Auth: NextAuth v5 + JWT middleware
6. MinIO service
7. CRUD API: projects, sources, show-bibles

### Этап 2: Ingestion (Фаза 1)
8. PDF ingestion (Marker)
9. Web ingestion (Jina → FireCrawl → Playwright)
10. Video/Audio ingestion (Deepgram)
11. Structural chunker (structure-first, then normalize)
12. Embeddings service (OpenAI → Qdrant)
13. Canonicalizer
14. Scout (ContentMap)
15. Celery setup + ingestion tasks
16. Frontend: Source uploader + status

### Этап 3: Show Setup + LangGraph Core (Фазы 0, 2-4)
17. LangGraph state + graph skeleton + PostgreSQL checkpointer
18. Phase 0: Show Configurator (ShowBible generation + presets)
19. Phase 2: Decomposer + Duration Fill Strategy
20. Phase 3.1: Hunter (closed-book retrieval)
21. Phase 3.2A: Librarian Fast (web search)
22. Phase 3.2B: Deep ingestion (Celery async)
23. Phase 4.1: Claim Builder
24. Phase 4.2: Verifier
25. Phase 4.3: Conflict Resolver
26. Real-time events: Redis pub/sub → WebSocket
27. Frontend: ShowBible editor

### Этап 4: Planning & Writing (Фазы 5-6)
28. Phase 5.1: Showrunner
29. Phase 5.2: Act Planners (Send() fan-out + Redis coordination)
30. Phase 5.3: Integrator
31. Phase 6.1: Screenwriter
32. Phase 6.2: Writer Revolt loop
33. Phase 6.3: Citation Auditor

### Этап 5: Audio (Фаза 7)
34. ElevenLabs TTS integration
35. Audio processing (pydub/ffmpeg, LUFS normalization)
36. Shownotes generator
37. Episode CRUD API + audio streaming

### Этап 6: Frontend Completion
38. Generation config page + cost estimator
39. Real-time progress UI
40. Episode player (wavesurfer.js + synced transcript)
41. Shownotes viewer
42. i18n (EN/RU)
43. Dashboard polish

### Этап 7: Production Quality
44. End-to-end tests
45. Token & Cost Governor (limits enforcement)
46. Error handling + retry strategies
47. Generation resume (checkpoint recovery)
48. Structured logging + monitoring
49. Cost tracking (actual vs estimated)

---

## 11. Принятые решения

1. **State-by-IDs строго:** script_segments хранятся в ScriptSegment table, в state — только script_segment_ids. Никаких текстов/объектов в LangGraph state. Аналогично research_tree → в EpisodeManifest, не в state.

2. **LangGraph checkpointing:** `langgraph-checkpoint-postgres` (AsyncPostgresSaver). Thread ID в `GenerationRun.langgraph_thread_id`. Resume endpoint: `POST /api/generations/{id}/resume`.

3. **Язык артефактов:** Все артефакты (Claims.statement, Beat Sheets, диалоги) на **целевом языке подкаста**. Verbatim quotes в EvidenceSpan — на языке оригинала источника. Screenwriter получает material уже на нужном языке.

4. **Cost estimation:** Формула: `f(duration_target, source_count, language)` → estimated tokens per phase × model pricing + estimated chars × ElevenLabs pricing. Показываем перед запуском, сравниваем с actual после.

5. **Chunking:** Structure-first (H2/H3/абзацы), потом normalize размер (256-1024 tokens). Heading hierarchy не ломается при split.

6. **Два режима web research:** Fast (в цикле, non-blocking, Tavily+Jina) vs Deep (Celery async, full ingestion). Act Planner **никогда не ждёт** deep ingestion.

7. **Adjacent expansion guardrail:** Строго по adjacency_policy из ShowBible. Макс topics: 2 (≤30m), 4 (≤60m), 6 (90+m). Каждый adjacent topic обоснован.

8. **Writer Revolt:** Haiku eval, Sonnet rewrite. Легитимные причины: boring scene + persona mismatch. Запрещено: censoring facts for character. Max 2 iterations/scene.

9. **ElevenLabs SSML:** Конвертация ssml_hints в поддерживаемый subset. Паузы/нормализация — pydub/ffmpeg post-processing.

10. **ShowBible:** Центральный объект "шоу". Пресеты для быстрого старта, custom для гибкости. Не таскаем в state — только show_bible_id, загружаем по запросу в каждой ноде.

11. **ShowBible override:** Generation API поддерживает show_bible_id (конкретная) или show_bible_override (патч audience/style/tone) → создаётся run-scoped ShowBible без перезаписи проектной.

12. **TempSource discipline:** Fast web evidence → Source(is_temporary=true). Citation Auditor: claim на ТОЛЬКО temporary sources → максимум OBSERVED + обязательный хедж. Deep ingestion upgrades → is_temporary=false, fully_ingested=true.

13. **DISPUTED quality gate:** Showrunner обязан выделить "debate moment" слот для каждого DISPUTED claim в основной линии. Manifest validation: все DISPUTED assigned → pass.

14. **Per-segment quality metrics:** ScriptSegment хранит fact_coverage_ratio, forbidden_phrase_hits, rigor_violations, persona_consistency_score. Позволяет debugging, UI подсветку, partial regeneration.

15. **Web security:** SSRF denylist, 50MB file limit, 30s fetch timeout, MIME validation, sandboxed parsers в изолированных worker'ах.

16. **TempSource lifecycle:** TTL привязан к generation_run + 7 дней grace. Cron cleanup. Temp sources исключены из основного retrieval (Hunter фильтрует is_temporary=false).

17. **raw_text в MinIO:** Полные тексты источников хранятся в object store (raw_text_path), не в Postgres. В PG только text_preview (500 chars) и metadata.

18. **VoiceProfile:** Хранится в ShowBible.voice_profiles. Для run-scoped ShowBible — копируется. Дефолт если не выбраны — первые 2 из ElevenLabs list.

19. **Revolt limits:** Макс 2 engagement + 1 persona-mismatch на сцену. Макс 4 на акт суммарно.
