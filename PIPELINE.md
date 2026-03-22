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
  - Защита от долгих зависаний: marker conversion запускается в отдельном subprocess с hard-timeout; при timeout/error subprocess принудительно завершается и используется локальный fallback extraction (pypdfium2, затем PyPDF2).

- Web → MediaWiki API (for `wikipedia.org/wiki/*`) → trafilatura → Jina Reader → httpx fallback, with auto PDF promotion (`.pdf` / `downloadpdf` direct URLs and nav-heavy Jina pages with same-host Download PDF links)

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
5. **PDF promotion path** — если URL PDF-like или Jina-страница портального типа (navigation-heavy) содержит same-host ссылку на `Download PDF`, система автоматически скачивает PDF и извлекает текст через `marker-pdf`/PyPDF2 fallback.

После extraction текст проходит quality gates:

1. **Gate A — Fetch health / anti-bot detection**
   - Вход: extracted text.
   - Проверки: `403`, `captcha`, `just a moment`, `security verification`, слишком короткий/пустой текст.
   - Выход: `web_quality_state` (`ok|low_signal|blocked`) + `web_quality_reasons[]`.
   - `blocked`: source помечается как failed.

2. **Gate B — Residual cleanup**
   - Вход: текст после Gate A.
   - Минимальная очистка: markdown link/image noise (для Jina path), JS-placeholder ссылки (`javascript:void(0)`), bullet-prefixed nav-линии (`* Skip to ...`), bare URL lines, повторяющиеся короткие строки, whitespace нормализация.
   - Важно: setext подчеркивания заголовков (`===/---`) сохраняются, если они формируют markdown heading-структуру.
   - Основная boilerplate-очистка уже сделана trafilatura на этапе extraction.

3. **Gate C — Metadata extraction gate**
   - Date extractor запускается только для `ok|low_signal` и при достаточном signal (`min_chars`, не пустой preview).
   - Парсинг structured output: tolerant parser (fenced JSON/partial wrapper), не `json.loads(raw)` напрямую.

**Tracing contract:** отдельные spans `ingestion.web_quality.gate_a|gate_b|gate_c` с компактным payload (state, reason, char_count, skipped=true/false).

**Manual source re-ingest:** `POST /api/sources/{source_id}/reingest` переводит source в `enqueued` и запускает ingestion заново; cleanup старых chunks + векторов выполняется в worker после успешного enqueue (не в API pre-queue path). Перед purge chunk-ов worker обнуляет `evidence_spans.chunk_id` для этого source, чтобы не ловить FK-конфликты.

### **1.2 Structural chunking**

- логические границы по markdown headings (H1-H6, ATX + Setext) + абзацы
- если markdown-заголовков нет: deterministic heading inference (outline prefixes / section labels / title-like lines) с anti-TOC guard
- **Child-heading transparency:** при проверке supporting body, дочерние заголовки (level > current) пропускаются, а не останавливают lookahead. Это позволяет корректно обрабатывать структуры вида Chapter → Section → body (Chapter не отбрасывается даже без промежуточного текста)
- **Article exclusion:** `article` исключён из structural heading labels — статьи (Article 1, Article 2...) это контент документа, а не структурные разделители
- merge маленьких chunk-ов только внутри одного `heading_path` (без смешивания разных секций)

- Parent--Child retrieval

### **1.3 Canonicalization & metadata**

- дедуп источников (URL/title/hash)

- извлечение published_at

- классификация типа источника

### **1.4 Scouting ("Content Map")**

- H1/H2/аннотации + (опционально) частотные сущности/термины
- source summary использует adaptive section cap (выше для few-source проектов, ниже для many-source проектов)
- lead-сентенции выбираются с фильтрацией обрывков overlap (предпочтение полным предложениям с корректным началом)
- lead для секции выбирается как лучший кандидат среди chunk-ов секции (а не «первый попавшийся»), чтобы навигационные префиксы не фиксировались как representative lead
- перед LLM входом применяется section-level noise filter для web-источников (навигационные/портальные секции удаляются) на основе language-agnostic структурных сигналов (heading-depth/prefix dominance, list/link density, prose-vs-index признаки)
- web-источники с высоким nav-noise ratio получают quality downweight; полное исключение применяется только при практически полном отсутствии retained prose-сигнала

- output: компактная карта корпуса (3--5k токенов)

---

## **Фаза 2: Query Decomposition → Research Tree (с учётом аудитории/шоу)**

### **2.1 Decomposer Agent (reasoning)**

Вход: user prompt + duration_target + ContentMap + ShowBible(show_bible_id)

Выход: **Research Tree** (2--3 уровня):

- angles (5--7)

- drill-downs (3--5 на angle)

- intent (`history|explain|compare|debate|case_study`)

- required evidence (строгие must-have asset types + min_count)

- expected evidence (nice-to-have asset types для усиления рассказа)

- quality_need (VERIFIED vs допустим OBSERVED/OPINION)

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
- keyword retrieval uses language-aware query rewrite based on the research question and sample corpus snippets, so lexical search follows corpus language rather than podcast language
- Hunter retrieval now adds intent-aware seed queries before vector/keyword merge, so `debate`/`compare`/`case_study` branches surface evaluative, contrastive, and episode-specific material instead of only topical background

- расширенный retrieval pool (`top-40`) + diversity re-ranking (penalty за повторяющиеся `source_id`/`parent_chunk_id`) перед extraction

- собирает canonical `EvidenceSpan`
- для каждого найденного evidence пишет `EvidenceProvenance`:
  - `question_id`
  - `branch_id`
  - `generation_run_id`
  - `asset_type` для текущего retrieval-context
  - `direct_or_contextual` для конкретного вопроса
  - `retrieved_by`

- intent из decomposition идёт в keyword rewrite и extractor prompt, чтобы Hunter различал explain/history/compare/debate/case_study вопросы уже на retrieval/extraction слое
- для `debate`/`compare`/`case_study` intent влияет не только на extraction prompt, но и на query generation: Hunter ищет pro/con positions, explicit contrasts, alternatives, named episodes, actors, outcomes
- extraction в Hunter теперь имеет retry path:
  - `extraction_failed` / exception -> narrower retry с меньшим числом чанков и явным directness-focus
  - `invalid_extractor_payload` -> strict-format retry
- `expected_evidence` больше не остаётся prompt-only hint: Hunter ведёт explicit tracking найденных soft editorial assets в debug/summary output

- формирует structured gap signal `research_gaps` по веткам:
  - `missing_assets` рассчитывается только по `required_evidence` (с учетом `min_count`)
  - `required_assets` (type/min_count/found_count)
  - `expected_assets`, `found_assets` как soft coverage сигнал
  - `intent` переносится в gap payload для downstream web-research
  - gap-записи формируются на уровне конкретного question (branch или child), чтобы не терять недопокрытые под-вопросы

### **3.2 Librarian / Web Researcher (Open-web, двух режима)**

#### **A) Fast Web Evidence (внутри цикла, non-blocking)**

- fast-web не использует raw question / raw claim как финальные search queries; вместо этого всегда делает короткий LLM query synthesis layer
- query synthesis работает для всех языков и обоих режимов (`hunter_gap_fill`, `verifier_corroboration`), а не только как translation/rewrite для non-English
- есть два fast-web режима с раздельными budgets/counters:
  - `hunter_gap_fill`
  - `verifier_corroboration`
- governor fast-web считает `processed gaps`, а не количество созданных evidence items; локальные ограничители остаются отдельно (`page_limit`, `max evidence per gap`)
- fetch path использует shared lightweight extractor stack: `mediawiki -> trafilatura -> jina` + `clean_web_text(...)`, а не сырой Jina-prefix
- extractor prompt получает не `content[:5000]`, а `excerpt_windows`: релевантные окна из очищенного текста
- verifier-mode fast-web больше не превращает `search_objective` и `missing_assets` в deterministic query-string glue; эти сигналы используются только как контекст для LLM query synthesis
- verifier-mode query synthesis не получает question-level `required_assets` / `expected_assets` / `missing_assets`; они остаются только background context для extractor, а не таргетом web search
- search results cheap-rerank'ятся до fetch по novelty, domain/authority quality, overlap с objective terms и source-family diversification
- verifier-gaps со взаимно пересекающимися `claim_ids` схлопываются в один corroboration target c union `claim_contexts` / `question_contexts` / coverage signals, чтобы не тратить несколько slots на одну и ту же группу слабых claims
- verifier fast-web budget теперь adaptive и upper-bounded: учитывает число remaining gaps и даёт extra priority interpretive intents (`debate` / `compare`), но не разрастается бесконтрольно
- если canonical source уже известен проекту и fully ingested с доступными chunks/content, fast-web skip'ает такой URL до fetch/extract и идёт к следующему novel candidate
- если source уже известен, но не fully ingested, fast-web может reuse preview только когда он уже достаточен; иначе не крутит слепой duplicate fetch loop
- verifier-loop fast-web работает в явном extractor mode `claim_corroboration`: primary target — слабые `claim_contexts`, а question-level `required/expected/missing assets` остаются только background context
- verifier-mode extractor payload не дублирует verbose `search_objective`; вместо этого он передаёт компактные `corroboration_goals` (`independent_second_source`, `direct_source_needed`, `stronger_authority_needed`) и sanitized `claim_contexts`
- verifier-mode post-filter больше не режет extracted items по question-level `target_missing_assets`; asset targeting остаётся только для `question_gap_fill`
- verifier-mode reranking и excerpt selection claim-anchor-first: broad question overlap сам по себе не должен поднимать страницу/окно в prompt
- trust-aware retention работает end-to-end, а не только в cheap rerank:
  - weak source families получают penalty
  - `_is_low_value_page` пишет explicit drop reason / overlap stats
  - higher-trust pages не должны так же легко отбрасываться, как unknown pages
- reference/bibliography-like excerpt windows отсекаются до extractor LLM по citation-style signals (`doi`, `vol.`, `pp.`, author-year patterns)
- stop signal в fast-web не деградирует в `search_failures`: `StepExecutionStopped` пробрасывается вверх и останавливает весь node
- fast-web summary/accounting различает `accepted_item_count`, `new_canonical_evidence_count`, `new_provenance_links_count`, `new_source_count`; realtime `evidence_found` считается по run-usable linked evidence, а не только по brand-new canonical quotes
- затем сохранить canonical `EvidenceSpan` + `EvidenceProvenance`

- web extractor получает `question_intent` и тоже помечает evidence как `direct` или `contextual`; для verifier-mode он больше не обязан искусственно подгонять evidence под question-level asset accounting

- применяем для OPINION/REPORT, OBSERVED facts, quick triangulation

- лимиты на число источников/акт, penalize низкокач домены

- приоритет задаётся `research_gaps` от Hunter (целенаправленное закрытие недостающих `asset_type`), эвристика по `priority` остаётся fallback
- при включённом `WEB_EXTRACTOR_DEBUG_DUMP_DIR` fast-web пишет per-gap и summary JSON dumps:
  - selected gaps
  - query variants
  - skipped known URLs / page handling
  - excerpt windows
  - accepted/rejected evidence

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

- собирает branch-based batches по `EvidenceProvenance`, а не режет flat array по фиксированному количеству rows
- если verifier-loop вернул `research_gaps`, Claim Builder делает targeted rebuild только для затронутых branch groups и сохраняет unaffected claims вместо полного глобального rebuild’а
- batching считает budget по реальному prompt payload; длинные quotes упаковываются адаптивно, а не слепо через fixed `[:700]` prefix
- в prompt передаёт не только quote, но и:
  - branch context (`branch_question`, `branch_intent`, question contexts)
  - source metadata (`title/url/type/domain_reliability/authority_type/is_temporary`)
  - compact lineage summary (`branch_ids/question_ids/intents/retrieved_by`)
  - contextual asset types из `EvidenceProvenance` для текущей branch-group
  - `direct_or_contextual` по provenance, когда он доступен
- при сохранении claim получает semantic branch/question assignment из support-lineage question contexts, а не только broad batch branch

- сохраняет narrative "сок": illustrative evidence не теряется
- prompt ужесточён против semantic overshoot:
  - partial/truncated quotes не расширяются до более сильных claims
  - contextual framing не апгрейдится в stronger fact без явной пропозиции
  - claims строятся как compact factual backbone для verifier/planner/writer, а не как exhaustive inventory каждого извлекаемого факта
  - по умолчанию prompt предпочитает fewer, stronger claims и merge'ит evidence по shared proposition core вместо `one span -> one claim`
  - supporting details (даты, числа, qualifiers, examples) не должны автоматически становиться отдельными claims, если не меняют downstream meaning
  - ANECDOTE / CASE / EXPERT_QUOTE по умолчанию идут в illustrative, если не несут самостоятельную writer-usable proposition
- branch intent влияет на shape claims:
  - `debate` -> arguments / tradeoffs / criticisms / attributed positions
  - `compare` -> explicit differences / alternatives / side-by-side contrast
  - `case_study` -> concrete actors / actions / outcomes / consequences
- один и тот же `ClaimEvidence` может быть:
  - factual support
  - contradiction
  - illustration

### **4.2 Global Claim Dedupe**

- после локальной генерации claims запускается deterministic global dedupe
- схлопываются exact и conservative near-exact duplicates
- evidence links и narrative flags мерджатся до verifier

### **4.3 Verification / Triangulation**

- FACT в основной линии стремится к VERIFIED:
  - 2+ независимых canonical sources, или
  - 1 strong primary/high source при прямом textual support

- verifier считает независимость по canonical source identity (`source.id` / `is_duplicate_of`), а не по сырым source rows
- verifier видит provenance question contexts (`question`, `intent`, `direct_or_contextual`) и различает factual backbone vs contextual framing на prompt-слое
- verifier больше не режет каждый supporting quote до фиксированных `300` символов; batching ограничивается char-budget’ом на batch
- после `dedupe_claims` verifier работает incremental:
  - `build_claims` пишет rebuild scope (`rebuilt_claim_ids`, `preserved_claim_ids`, `targeted_branch_ids`)
  - `dedupe_claims` пишет touched scope (`dedupe_deleted_claim_ids`, `dedupe_merged_into_claim_ids`, `dedupe_untouched_claim_ids`)
  - verifier пересчитывает только claims, у которых реально изменился verifier payload
- для этого на `Claim` хранится `verification_fingerprint`; unchanged claims сохраняют прежний `verification_level` без повторного LLM-вызова
- verify scope учитывает полный union `claim_ids` из merged research gaps, а не только lead claim каждой gap-group
- targeted rebuild scope для claim builder тоже расширяется по merged gap lineage: rebuild включает все branch/question contexts, разделяющие overlap-claims в verifier gaps
- targeted rebuild умеет переносить прежний verification state/fingerprint на rebuilt claim, если statement+evidence signature не изменился и claim не был прошлым unresolved gap

- claims на only-temporary evidence не могут быть `VERIFIED`

- OPINION/REPORT: достаточно 1 источника, но обязательны атрибуция и контекст
- attribution должен быть содержательным, а не placeholder вида "источник" / "неустановленный эксперт"

- DISPUTED: обе стороны + причины расхождений

- verifier при `needs_research` пишет свежие `research_gaps` по реальным provenance-парам `(branch_id, question_id)`, без synthetic Cartesian product, и fast-web loop работает уже по ним, а не по старым gap’ам Hunter
- verifier gaps теперь несут не только branch/question, но и search guidance для loop:
  - `claim_statement`
  - `support_strength`
  - `search_objective`
  - `existing_support_summary`
  - `required_assets` / `expected_assets` / `missing_assets` из research context, когда они доступны
- fast-web claim-corroboration path использует эти поля как input context, но не обязан механически копировать их в extractor prompt; top-level verbose `search_objective` заменяется compact corroboration goals layer
- verifier reasoning принудительно держится коротким и evidence-bound: без спекуляций о "future dates", скрытых metadata-анomalies и прочих неявных выводов
- showrunner больше не генерирует synthetic numeric conflicts из соседних claims; planning видит только реальные upstream conflict briefs / disputed claims

- при включённом `WEB_EXTRACTOR_DEBUG_DUMP_DIR` verifier пишет JSON dumps:
  - prefiltered claims
  - LLM batch input/raw output/parsed output/final decisions
  - run summary с `research_gaps`

### **4.4 Contradiction Resolver**

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

- Showrunner получает richer claim payload:
  - `supporting_evidence_count`
  - `illustrative_evidence_count` / `has_illustrative`
  - `branch_ids`, `question_ids`, `intents`
  - `direct_support_count`, `contextual_support_count`
  - `source_diversity` + lightweight quality hints
- Showrunner теперь планирует и editorial contract каждого акта:
  - `story_role`
  - `purpose`
  - `internal_arc`
  - `opening_question`
  - `ending_turn`
  - `focus_branch_ids`
- после normalization вводится density cap на claim pool per act, чтобы акты вели себя как curated editorial layer, а не как почти полный research dump
- disputed claims и claims из real/manual conflicts сохраняются через density cap и не могут быть молча выкинуты до conflict validation
- planning payloads now carry `output_language`, so showrunner and act planner keep human-readable output in the podcast language instead of drifting into English
- preserve-path для disputed/conflict claims остаётся bounded самим act cap: конфликтно-насыщенные акты не пробивают cap полностью
- showrunner prompt теперь явно ставит semantic novelty выше branch coherence: если claims из разных веток по сути переупаковывают один и тот же механизм, только один должен стать fresh explanatory backbone, а более поздний акт обязан двигаться через stress test / cost / contrast / legal framing / payoff, а не через повторное teaching; один исторический эпизод или институциональное решение обычно должны иметь один главный dramatic home, а comparative outside material не должно разрастаться в набор случайных мини-историй

- при включённом `WEB_EXTRACTOR_DEBUG_DUMP_DIR` Showrunner пишет JSON dump с input payload, raw/parsed LLM output и normalized act plan

### **5.2 Act/Sect Planners (параллельные)**

- пишут Beat Sheet (таймкоды/сцены)
- получают claims не как flat statement list и не как lossy `evidence[:20]`, а как compact claim bundles:
  - claim-level `type`, `verification`, `has_illustrative`, `illustrative_evidence_count`, `direct_support_count`, `source_diversity`
  - `branch_ids` / `intents`
  - `support_bundle.direct` (1 лучший factual quote)
  - `support_bundle.illustrative` (0-1 vivid quote when available)
- дополнительно получают act-level story contract, `branch_focus_summary`, `conflict_count` и `scene_count_guidance`, чтобы строить сцены как движение внутри акта, а не как flat list of explanation beats
- planner теперь нацелен на compact beat cards, а не на длинные pseudo-script paragraphs; он просит модель сначала выбрать короткую `scene_strategy`, явно запрещает default-скелет `hook -> explanation -> case_study -> takeaway`, трактует `case_study` как optional scene function, держит все human-readable поля в `output_language` и получает compact `prior_acts_primary_claims`, чтобы later acts знали, что earlier acts уже установили как primary material
- prompt act planner теперь жёстче различает `same mechanism in new words` и `genuinely new layer`: later acts должны добавлять stress test / cost / contrast / legal lock-in / consequence, соседние сцены не должны повторно прогонять одну procedural chain под разными ярлыками, historical acts должны по умолчанию оставаться хронологически читаемыми, а callback-heavy endings обязаны начинать beat card с нового move, а не с recap механики
- после normalizer act planner детерминированно делит повторно использованные claims на `primary_claim_ids` и `callback_claim_ids` по порядку сцен, а для callback-heavy beats задаёт `scene_job` (`reframe`, `cost`, `contrast`, `bridge`, `synthesize`, `apply`, `stress_test`), чтобы downstream writing видел, какой move сцена должна сделать
- `hook` / `explanation` / `case_study` сцены не могут остаться callback-only без нового primary claim и в таком случае срабатывает отдельный quality-retry budget (`ACT_PLANNER_QUALITY_RETRIES = 2`); transport/timeout/parse failures используют отдельный request/output retry budget (`ACT_PLANNER_REQUEST_RETRIES = 2`), поэтому сетевой stall не съедает последний шанс на quality retry. Callback-only ending scenes сохраняют structural limits на `<=2` callback claims и ending-friendly `scene_job`; длинный ending description теперь остаётся advisory signal, а не hard fail, потому что length alone слишком brittle across languages/corpora
- если сцена выбрала claim, но LLM не приклеил `evidence_ids`, act planner сначала детерминированно подставляет bundled claim support и только потом идёт в `vector_search` rescue
- Anthropic LLM calls теперь обёрнуты в общий hard timeout (`LLM_REQUEST_TIMEOUT_SECONDS`, default `300`), чтобы Celery worker не зависал бесконечно на stalled provider request

- при включённом `WEB_EXTRACTOR_DEBUG_DUMP_DIR` каждый act planner сначала пишет ранний `started` dump до первого LLM-вызова, а затем финальный JSON dump с prompt payload, attempt history, raw/parsed outputs, normalized beat sheet и результатом evidence gap fill

### **5.3 Integrator**

- проверяет межактовые переходы, pacing и complexity на уровне всего эпизода
- пишет короткие `transition_note` в последнюю сцену каждого акта, когда переход действительно нужен
- теперь работает через stricter structured-output contract с retry, чтобы длинный integrator output не деградировал в silent no-op из-за truncation
- при включённом `WEB_EXTRACTOR_DEBUG_DUMP_DIR` пишет JSON dump в `planning_integrator` с prompt payload, attempt history и реально применёнными переходами

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

- использует роли хостов как мягкие тенденции, а не как жёсткую схему `A объясняет / B спрашивает`

- подаёт факты через подходящие narrative assets

- использует не только Claims, но и illustrative_evidence_ids (анекдоты, метафоры, кейсы)

- получает компактный continuity context вместо полного act contract:
  - `act_title`
  - `previous_scene`
  - `previous_act_summary` для первых сцен новых актов
  - `already_covered_claims`
  - `already_covered_claims_global`
  - `new_information_goal`
  - `transition_note`
  - `scene_claim_mode`
  - `scene_job`
  - `callback_claim_ids`
  - `recent_scene_starts`

- держит dialogue в `language` подкаста, а не дрейфует в английский или смешанный output

- использует `description` как main job сцены, а `transition_note` только как лёгкий landing/bridge

- если текущая сцена пересекается с локально или глобально уже покрытым материалом, делает callback и двигается к новому смыслу вместо полного re-explain

- continuity для `previous_scene` и первых сцен новых актов строится от фактически написанного диалога:
  - `previous_scene` несёт `what_listener_now_knows`, реально прозвучавшие `claim_ids`, `mechanism_anchors` и `last_line` предыдущей сцены
  - `previous_act_summary` по возможности собирается из реально использованных `claim_ids` и последней реплики предыдущего акта, а не только из planning metadata

- `new_information_goal` задаёт delta этой сцены: какой новый смысл, следствие или угол она должна добавить поверх уже рассказанного

- в локальный writer prompt попадают только `primary_claim_ids` текущей сцены; `callback_claim_ids`, `scene_claim_mode` и `scene_job` нужны как continuity signal, а global continuity memory дополнительно несёт compact generic `mechanism_anchors`, чтобы следующие сцены и акты не reteach'или уже установленный механизм как новый факт
- `scene_job` даёт callback-heavy сценам положительный контракт:
  - `reframe`
  - `cost`
  - `contrast`
  - `bridge`
  - `synthesize`
  - `apply`
  - `stress_test`

- `recent_scene_starts` — детерминированный список недавних opener-shapes внутри текущего акта; writer использует его как soft anti-repetition cue, чтобы сцены не начинались одним и тем же маркером снова и снова

- если предыдущая сцена уже сформулировала punchline, новая сцена должна по возможности продвигать разговор дальше, а не перефразировать тот же вывод в первых 1-2 репликах

- writer варьирует discourse shapes абстрактно, а не через словарные blacklist'ы:
  - не каждый challenge начинается как interruptive opener
  - не каждое clarification exchange обязано идти по схеме question -> explicit confirmation -> explanation
  - direct continuation, answer, or correction часто лучше, чем очередное подтверждение-маркер

- speaker labels в final dialogue нормализуются к ASCII `A/B`, даже если модель вернула кириллические `А/Б` или формы вроде `Host A`

- не default'ится в строгий `ABAB` exchange и не повторяет одни и те же opener-фразы scene after scene

- при точных датах / юридических нормах / институциональных правилах может использовать лёгкую атрибуцию (`по данным регулятора`, `как сказано в законе`, `согласно официальным правилам`) вместо полностью безымянного narratorial tone

- если `previous_scene.last_line` даёт естественный bridge, новая сцена может начинаться как продолжение уже начатой мысли, а не как полный conversational reset

- ставит теги для TTS (direction/mood), но без ломания смысловой структуры

- при включённом debug dump пишет per-scene JSON-артефакты в `tmp/debug/screenwriter`:
  - input payload
  - все LLM attempts
  - raw/parsed output
  - финальный dialogue

### **6.2 Writer Revolt (расширенные причины)**

Теперь это не просто `score < 0.6`, а **boundary-aware dialogue audit**:

- видит текущую сцену плюс компактный boundary context от предыдущей сцены
- сохраняет structured findings (`type`, `severity`, `scope`, `description`, `suggestion`) в `script_segments.audit_findings`
- medium-severity проблемы с scope `scene_polish` пытается чинить inline через scene-level polish pass
- prompt для polish получает `scene_job`, чтобы polish не ломал intended move сцены
- только high-severity `scene_rewrite` проблемы отправляют сцену назад в `screenwriter` через `audit_status = REVISED`

Легитимные причины:

- "Сцена скучная --- нужен CASE/ANECDOTE/CONTRAST" для конкретного claim
- "rigid_turn_taking" — сцена звучит механически из-за слишком жёсткого чередования реплик
- "repetitive_agreement_shape" — подтверждения звучат однотипно scene after scene
- "repetitive_challenge_shape" — возражения/уточнения повторяют один и тот же discourse move
- "scene_opening_echo" — сцена открывается повтором punchline предыдущей сцены
- "teacherly_exposition" — сцена объясняет верно, но слишком лекционно и без conversational movement
- "boundary_restatement" — opener новой сцены рестейтит то, что boundary context уже успел установить

- **Persona mismatch (разрешено):**
  - "Challenger не поверит без stronger VERIFIED пруфа"

  - "Нужен контраргумент/другая сторона, чтобы спор был честным"

    Запрещено:

- "Уберём факт потому что не в характере" (персона не должна переписывать истину)

### **6.3 Citation Auditor (обязательный)**

- каждый factual statement должен ссылаться на Claim ID

- проверка формулировок по verification_level

- проверка соответствия rigor_policy и forbidden_phrases

- теперь сохраняет structured citation findings (`type`, `severity`, `scope`) в `script_segments.audit_findings`

- detection-pass делает Haiku; Sonnet остаётся только для `citation_repair`

- detection-pass получает не весь episode claim universe, а scene-local claim subset: `dialogue.claim_ids` + manifest-assigned scene claims

- `missing_claim_link` не должен срабатывать на чисто разговорных / риторических / synthesis линиях, если они не вводят новый factual assertion

- medium/high severity issues запускают scene-level `citation_repair` pass

- `citation_repair` теперь ориентирован не только на rigor, но и на spoken-script quality:
  - prompt explicitly keeps podcast voice instead of drifting into legalese / academic prose
  - repair strategy order is `reuse existing claim link -> local hedge/narrowing -> softer nearby wording -> deletion only as last resort`
  - scene context now carries `is_final_scene_in_act` / `is_final_scene_in_episode`, so mid-episode bridges should not mutate into fake sign-offs like `об этом в следующий раз`
  - scene-level repair gets flagged-line payloads and is told to focus edits on flagged lines while keeping clean lines as intact as possible
  - prompts preserve named actors / agency and include contrastive examples against teaser-language, double hedging, and abstract role-speak

- audit-pass имеет bounded retry на transient timeout/parse failures; если все попытки исчерпаны, сцена fail-closed превращается в blocking `audit_failure`

- после LLM-аудита есть cheap post-filter для неутверждающих вопросительных реплик: only a tiny whitelist of genuinely generic questions (`Что произошло?`, `Что дальше?`) downgrade to `record`; recap / confirmation questions and loaded wh-questions with embedded factual premises stay actionable instead of fail-open. `unverified_stated_as_fact` on a pure generic question no longer triggers pointless `line_repair`

- repaired scene повторно аудируется на том же narrowed context; repair принимается по blocker-focused rules:
  - сначала используется `line_repair`: модель переписывает только flagged line indexes, а auditor делает mechanical splice и сохраняет все unflagged lines без изменений
  - `scene_repair` используется только как fallback, если проблема шире локального patch
  - original blocking issues must be resolved/downgraded
  - no new blocking issues
  - required claim coverage must be preserved for assertive content; only repaired question lines may drop stale `claim_ids`
  - line-count drift must stay bounded for scene-level repairs (no free-form scene rewrite disguised as rigor repair)

- scenes с исходными `scope=block` findings получают ещё один repair attempt с compact retry feedback, чтобы один неудачный Sonnet rewrite не ронял весь дорогой run

- unresolved issues со scope `block` после repair **блокируют переход в audio**, вместо молчаливого continuation; default runtime mode is fail-fast, so node stops at the first unresolved blocker instead of auditing the rest of the episode

- при включённом debug dump пишет JSON-артефакты в:
  - `tmp/debug/citation_audit`
  - `tmp/debug/citation_repair`

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
- `POST /api/generations/{run_id}/steps/rerun` — переисполнить выбранный уже выполненный node; если `target_node` не передан, берется последний выполненный шаг.
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
