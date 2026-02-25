# **Архитектура мультиагентной системы для Long-Form подкастов (Premium Quality v2.3)**

## **Цель**

Автономно создавать подкасты **любой длительности, заданной пользователем** (15/30/60/90+ минут), в формате **2 ведущих**, с приоритетом **глубины**, на основе:

1.  загруженного пользователем корпуса (PDF/статьи/видео/сайты),

2.  **open-web** (самостоятельный поиск и фактчек) **без блокировок** пайплайна.

Ключевое продуктовое правило: если тема узкая под заданную длительность, система **уходит вглубь** и/или делает **контролируемое смежное расширение** (adjacent expansion) --- при этом **не льёт воду**: всё должно быть обеспечено Claims/Evidence.

---

## **Фаза 0: Show Setup (NEW) --- "The Show Bible"**

Пользователь задаёт:

- topic

- duration_target

- audience_level: Beginner | Casual | Industry Professional

- show_style: пресет или custom (например: Investigative, Conversational, Academic Debate, Explainer)

- (опционально) tone_preference: serious/light, humor level, profanity allowed, etc.

### **Show Configurator Agent (создаёт Show Bible)**

Создаёт объект **ShowBible** (или берёт из пресета), сохраняет в хранилище и возвращает **show_bible_id**.

> Важно для прода: **ShowBible не таскаем в LangGraph state целиком**. В state --- только show_bible_id. Любая нода подгружает bible по ID (State-by-IDs правило).

---

## **Объекты данных**

### **HostPersona**

- name

- role: например Guide (объясняет/структурирует) или Challenger (сомневается/дожимает доказательства)

- vocabulary_rules: академично/простыми словами/слэнг/табу

- interaction_style: перебивает/резюмирует/задаёт "наивные" вопросы/иронизирует

- explanation_behavior: как объясняет сложное (через аналогии, примеры, определения)

- constraints: чего избегать (политика, шейминг, токсичность и т.п.)

### **VoiceProfile (отдельно от персоны)**

- tts_provider, voice_id

- pace, pitch, stability и др. (провайдер-специфично)

### **ShowBible (ядро "шоу")**

- audience_profile: что аудитория уже знает / что объяснять

- tone_and_mood: serious/light/comedic/investigative

- show_style: формат (не "копия конкретных ведущих", а тип шоу)

- hosts: List[HostPersona]

- voice_profiles: List[VoiceProfile]

- forbidden_phrases: запрет LLM-клише ("В заключение хочу сказать...", "Как известно...", и т.п.)

- rigor_policy: требования к строгости (например: "ключевые FACT в основной линии должны быть VERIFIED")

- adjacency_policy: правила "ухода в сторону" (только если усиливает понимание core / механизм / конфликт / кейс)

- explanation_policy: как объяснять сложное под выбранную аудиторию (термин → определение → пример; цифры → интуитивный перевод)

- format_rules: допустимая длина реплик, частота резюме, допустимые шутки, теги для TTS и т.д.

---

### **EvidenceSpan**

Поля:

- evidence_id

- source_id, url, source_type (pdf/web/video/journal/gov/etc)

- published_at (если есть)

- locator (page/section/timestamp/CSS selector)

- verbatim_quote (обязательно)

- start_idx/end_idx

- reliability_hint

- asset_type: STATISTIC | ANECDOTE | EXPERT_QUOTE | DEFINITION | MECHANISM | CASE | TIMELINE | CONTRAST

- narrative_value_score (0--1)

- tellability_notes (как рассказать)

### **Claim (логический узел)**

- claim_id

- claim_type: FACT | OPINION | REPORT

- verification_level: OBSERVED | VERIFIED | DISPUTED | UNVERIFIED

- statement

- attribution (обязательно для OPINION/REPORT: кто/где/когда)

- as_of_date

- supporting_evidence_ids: [...]

- counter_evidence_ids: [...] (если DISPUTED)

- illustrative_evidence_ids: [...] (анекдоты/кейсы/цитаты для подачи)

- notes (методология/оговорки)

#### **Правила речи ведущих (встроенные)**

- OPINION/REPORT: всегда через атрибуцию ("Karpathy считает...", "в интервью X сказал...")

- FACT:
  - VERIFIED → можно говорить уверенно

  - OBSERVED/UNVERIFIED → только осторожно ("по данным источника...", "в отчёте указано...")

  - DISPUTED → проговорить конфликт + причины (дата/выборка/определения)

---

## **Фаза 1: Ingestion & Indexing**

### **1.1 Cascade ingestion**

- PDF → Marker → Markdown

- Web → MediaWiki API (for `wikipedia.org/wiki/*`) → trafilatura → Jina Reader → httpx fallback

- Video/Audio → Deepgram + diarization

### **1.1b Text Normalization**

Между extraction и chunking — слой нормализации (`app/ingestion/normalize.py`):

- **Universal:** Unicode NFC, whitespace normalization (NBSP→пробел, collapse множественных пробелов, 3+ newlines→2)
- **PDF-only:** hyphenation repair (`inter-\nnational` → `international`), page number stripping, boilerplate removal (повторяющиеся строки: headers/footers/copyright)

Raw text сохраняется в MinIO до нормализации (audit trail). Chunker получает уже нормализованный текст.

### **1.1c Web Extraction & Quality Gates**

Web extraction использует каскад extractors:

1. **MediaWiki API** (special-case для `wikipedia.org/wiki/*`) — забирает чистый article text без UI-навигации и конвертирует wiki headings в markdown-заголовки для chunker.
2. **trafilatura** (primary для остальных сайтов) — локальная библиотека, извлекает основной контент из HTML на уровне DOM. Удаляет навигацию, рекламу, footer, boilerplate автоматически. Также извлекает metadata (title, date).
3. **Jina Reader** (fallback) — hosted API с headless browser, обрабатывает JS-heavy страницы. Возвращает markdown.
4. **httpx + HTMLParser** (last resort) — stdlib парсер, убирает `<script>/<style>/<nav>/<footer>`.

После extraction текст проходит quality gates:

1. **Gate A — Fetch health / anti-bot detection**
   - Вход: extracted text.
   - Проверки: `403`, `captcha`, `just a moment`, `security verification`, слишком короткий/пустой текст.
   - Выход: `web_quality_state` (`ok|low_signal|blocked`) + `web_quality_reasons[]`.
   - `blocked`: source помечается как failed.

2. **Gate B — Residual cleanup**
   - Вход: текст после Gate A.
   - Минимальная очистка: markdown link/image noise (для Jina path), bare URL lines, nav-линии, повторяющиеся короткие строки, whitespace нормализация.
   - Основная boilerplate-очистка уже сделана trafilatura на этапе extraction.

3. **Gate C — Metadata extraction gate**
   - Date extractor запускается только для `ok|low_signal` и при достаточном signal (`min_chars`, не пустой preview).
   - Парсинг structured output: tolerant parser (fenced JSON/partial wrapper), не `json.loads(raw)` напрямую.

**Tracing contract:** отдельные spans `ingestion.web_quality.gate_a|gate_b|gate_c` с компактным payload (state, reason, char_count, skipped=true/false).

### **1.2 Structural chunking**

- логические границы (h2/h3/абзацы)

- Parent--Child retrieval

### **1.3 Canonicalization & metadata**

- дедуп источников (URL/title/hash)

- извлечение published_at

- классификация типа источника

### **1.4 Scouting ("Content Map")**

- H1/H2/аннотации + (опционально) частотные сущности/термины

- output: компактная карта корпуса (3--5k токенов)

---

## **Фаза 2: Query Decomposition → Research Tree (с учётом аудитории/шоу)**

### **2.1 Decomposer Agent (reasoning)**

Вход: user prompt + duration_target + ContentMap + ShowBible(show_bible_id)

Выход: **Research Tree** (2--3 уровня):

- angles (5--7)

- drill-downs (3--5 на angle)

- intent (explain/compare/history/mechanism/numbers/controversy/cases)

- expected evidence type

- quality_need (VERIFIED vs допустим OBSERVED/OPINION)

- explanation hints по audience_profile (что точно разъяснить, какие термины определить)

### **2.2 Duration Fill Strategy (управляемая "глубина")**

Слои добора длительности по порядку (активируются по необходимости):

1.  Definitions & scope

2.  Core mechanisms

3.  History/evolution

4.  Case studies (2--4)

5.  Edge cases / failure modes

6.  Debates & contradictions

7.  Adjacent expansion (строго по adjacency_policy)

8.  Practical takeaways

Правило: если слой активирован --- он должен быть обеспечен Claims/Evidence (не "вода").

---

## **Фаза 3: Targeted Research (Closed-book + Open-web, двухскоростной)**

### **3.1 Hunter Agent (Closed-book)**

- hybrid retrieval (vector + keywords)

- собирает EvidenceSpans + предварительные Claims

### **3.2 Librarian / Web Researcher (Open-web, двух режима)**

#### **A) Fast Web Evidence (внутри цикла, non-blocking)**

- быстрый reader/snippets → temp_source + EvidenceSpans

- применяем для OPINION/REPORT, OBSERVED facts, quick triangulation

- лимиты на число источников/акт, penalize низкокач домены

#### **B) Deep Web Ingestion (асинхронно)**

Цель: "дотащить" первички и нормализовать в индекс.

- pipeline: download → Marker/парсинг → chunking → embeddings → Qdrant

- запускается как Celery job и **не блокирует** акт

- когда завершён:
  - обновляет verification level (например OBSERVED → VERIFIED)

  - добавляет локаторы/точные цитаты/metadata

---

## **Фаза 4: Claim Building & Fact-Checking**

### **4.1 Claim Builder**

- агрегирует Evidence → Claims

- сохраняет "сок": illustrative_evidence_ids, asset_type

### **4.2 Verification / Triangulation**

- FACT в основной линии стремится к VERIFIED:
  - 2+ независимых подтверждения или 1 первичный авторитетный источник

- OPINION/REPORT: достаточно 1 источника, но обязательны атрибуция и контекст

- DISPUTED: обе стороны + причины расхождений

### **4.3 Contradiction Resolver**

- формирует Conflict Brief:
  - что конфликтует

  - почему (методология/дата/определения)

  - как проговорить честно в сценарии

---

## **Фаза 5: Hierarchical Planning (эластичные секции под duration_target)**

### **5.1 Showrunner (Macro)**

Вход: duration_target + ShowBible + Claims + Conflicts + Duration Strategy

Выход: Episode Manifest:

- **количество актов/секций: 2--6** (не фиксировано)

- темп/структура под show_style (investigative vs conversational vs debate)

- квоты на секцию:
  - X VERIFIED facts (если применимо)

  - Y attributed opinions

  - минимум 1 narrative asset (ANECDOTE/CASE/QUOTE) для "живости"

  - конфликт/контраргумент (если есть)

  - takeaway

### **5.2 Act/Sect Planners (параллельные)**

- пишут Beat Sheet (таймкоды/сцены)

- если не хватает материала:
  - Hunter (closed)

  - Librarian Fast (open)

  - Deep ingestion --- в фоне (не блокирует)

### **5.3 Integrator**

- стыки актов

- анти-повторы

- контроль нарастания сложности и "темп/внимание"

---

## **Фаза 6: Writers Room (персоны + аудитория + стиль)**

### **6.1 Screenwriter**

Вход: BeatSheet + Claims + ShowBible

Пишет диалог:

- строго следует ролям хостов (Guide/Challenger)

- подаёт факты через подходящие narrative assets

- использует не только Claims, но и illustrative_evidence_ids (анекдоты, метафоры, кейсы)

- ставит теги для TTS (direction/mood), но без ломания смысловой структуры

### **6.2 Writer Revolt (расширенные причины)**

Легитимные возвраты планировщику:

- "Сцена скучная --- нужен CASE/ANECDOTE/CONTRAST" для конкретного claim

- **Persona mismatch (разрешено):**
  - "Challenger не поверит без stronger VERIFIED пруфа"

  - "Нужен контраргумент/другая сторона, чтобы спор был честным"

    Запрещено:

- "Уберём факт потому что не в характере" (персона не должна переписывать истину)

### **6.3 Citation Auditor (обязательный)**

- каждый factual statement должен ссылаться на Claim ID

- проверка формулировок по verification_level

- проверка соответствия rigor_policy и forbidden_phrases

- если нарушение → downgrade формулировки или возврат в ресёрч

---

## **Фаза 7: Audio & Shownotes**

- TTS по VoiceProfiles, нормализация

- shownotes: Claim → Evidence (цитаты/локаторы/ссылки/даты) + блок "что спорно/где расхождения"

---

## **Memory & State Management (прод-правила)**

1.  **State by IDs:** в LangGraph state только \*\_id списки (claim_ids, evidence_ids, source_ids, show_bible_id, manifest_id)

2.  **Lazy loading:** тексты/цитаты materialize только в Writer/Auditor и только top-K

3.  **Governors:** лимит циклов writer↔planner, лимит fast-web источников на акт, лимит concurrent deep-ingestion jobs

---

## **Observability & Tracing (runtime)**

Текущая реализация поддерживает end-to-end tracing через LangSmith (опционально по env):

- `LANGSMITH_TRACING=true`
- `LANGSMITH_API_KEY`
- `LANGSMITH_PROJECT` (опционально, по умолчанию `podgen`)

Инструментированные уровни:
- Celery roots: generation/ingestion/deep-ingestion/cleanup
- LangGraph root (`pipeline.execute`) и каждый node (`pipeline.node.*`)
- LLM/embedding вызовы (`llm.call_*`, `llm.get_embeddings`)
- research/ingestion tools (`research.tool.*`, `ingestion.*`)

Контракт: в трейсы пишутся сжатые метаданные (ID, counts, status, размеры), а не полные тексты артефактов.

---

## **Режимы запуска генерации (runtime)**

- `execution_mode=full` (дефолт): запускается полный pipeline от `setup_show` до `build_shownotes`.
- `execution_mode=step`: полный граф автоматически не запускается; выполнение идет по одному node.

Для `step` режима:
- `POST /api/generations/{run_id}/steps/next` — выполнить следующий node.
- `POST /api/generations/{run_id}/steps/rerun` — переисполнить последний выполненный node.
- `POST /api/generations/{run_id}/steps/stop` — остановить текущий/очередной step action.
- `rerun` доступен только после как минимум одного успешно завершенного шага (нужен checkpoint + `last_node`).
- после каждого шага и после подтвержденной остановки публикуется realtime-событие `generation_paused` с `executed_node` и `next_node` (дополнительно может содержать `stopped=true` и `reason`).

Назначение: отладка и итеративная проверка фаз без стоимости полного прогона.

---

## **Сигналы качества (простые метрики)**

- % factual lines covered by Claim IDs

- доля VERIFIED среди ключевых FACT claims

- количество DISPUTED, которые проговорены (а не замолчаны)

- плотность narrative assets (CASE/ANECDOTE/QUOTE) по секциям

- соответствие audience_level (термины объяснены/не пережёваны)
