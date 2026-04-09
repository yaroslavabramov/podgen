# **Архитектура мультиагентной системы для Long-Form подкастов (Premium Quality v2.3)**

## **Цель**

Автономно создавать подкасты **любой длительности, заданной пользователем** (15/30/60/90+ минут), в формате **2 ведущих**, с приоритетом **глубины**, на основе:

1.  загруженного пользователем корпуса (PDF/статьи/видео/сайты),

2.  **open-web** (самостоятельный поиск и фактчек) **без блокировок** пайплайна.

Ключевое продуктовое правило: если тема узкая под заданную длительность, система **уходит вглубь** и/или делает **контролируемое смежное расширение** (adjacent expansion) --- при этом **не льёт воду**: всё должно быть обеспечено Claims/Evidence.

Неоспоримое инженерное ограничение:

- сервис должен оставаться универсальным и мультиязычным
- нельзя чинить пайплайн кейсовыми хаками под один эпизод, язык или корпус
- нельзя полагаться на системные словари, hand-built wordlists, phrase blacklists или прочие brittle эвристики, завязанные на конкретные формулировки
- допустимы только user/project-level ограничения вроде `forbidden_phrases` в ShowBible
- системные фиксы должны опираться на structured metadata, prompt contracts, issue taxonomy, repair strategy и acceptance logic, которые масштабируются между языками и корпусами

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
- если verifier-loop вернул `research_gaps`, Claim Builder делает targeted rebuild только для затронутых branch groups, но перед сохранением unaffected claims теперь revalidates их against current live branch/question scope; если старый claim pool больше не соответствует текущему дереву, node падает обратно в `full_rebuild`
- manual step rerun самого `build_claims` не использует preserved-claim incremental semantics: для UX это теперь fresh rebuild с заменой прежнего claim pool, а не скрытый internal patch старого claim pool
- `build_claims` теперь также пишет в state compact `dedupe_source_claims`: это canonical pre-dedupe claim pool для isolated rerun `dedupe_claims`
- batching считает budget по реальному prompt payload; длинные quotes упаковываются адаптивно, а не слепо через fixed `[:700]` prefix
- в prompt передаёт не только quote, но и:
  - branch context (`branch_question`, `branch_intent`, question contexts)
  - source metadata (`title/url/type/domain_reliability/authority_type/is_temporary`)
  - compact lineage summary (`branch_ids/question_ids/intents/retrieved_by`)
  - contextual asset types из `EvidenceProvenance` для текущей branch-group
  - `direct_or_contextual` по provenance, когда он доступен
- branch context теперь разделён на primary vs secondary/provenance question contexts: foreign-branch lineage остаётся доступной как background provenance, но не подаётся как permission to restate another branch backbone
- при сохранении claim получает semantic branch/question assignment из support-lineage question contexts, а не только broad batch branch

- сохраняет narrative "сок": illustrative evidence не теряется
- prompt ужесточён против semantic overshoot:
  - partial/truncated quotes не расширяются до более сильных claims
  - contextual framing не апгрейдится в stronger fact без явной пропозиции
  - claims строятся как compact factual backbone для verifier/planner/writer, а не как exhaustive inventory каждого извлекаемого факта
  - по умолчанию prompt предпочитает fewer, stronger claims и merge'ит evidence по shared proposition core вместо `one span -> one claim`, но explicit guardrails теперь запрещают сливать setup->response, regime A->later regime shift, legal rule->later application/result и другие distinct downstream moves в одну mini-scene claim
  - ambiguous claim shape теперь bias'ится в сторону narrower/split formulation, а не `prefer merge when unsure`; prompt даёт contrastive bad/good examples для compressed mini-scene claims
  - supporting details (даты, числа, qualifiers, examples) не должны автоматически становиться отдельными claims, если не меняют downstream meaning
  - ANECDOTE / CASE / EXPERT_QUOTE по умолчанию идут в illustrative, если не несут самостоятельную writer-usable proposition
- branch intent влияет на shape claims:
  - `debate` -> arguments / tradeoffs / criticisms / attributed positions
  - `compare` -> explicit differences / alternatives / side-by-side contrast
  - `case_study` -> concrete actors / actions / outcomes / consequences
- в claim-builder prompt `evidence_ids` теперь сокращаются до compact opaque labels и восстанавливаются после parse, так что LLM больше не должна перепечатывать длинные UUID ради evidence-link assignment
- один и тот же `ClaimEvidence` может быть:
  - factual support
  - contradiction
  - illustration

### **4.2 Global Claim Dedupe**

- после локальной генерации claims запускается двухслойный global dedupe:
  1. **Lexical layer** (deterministic): exact и conservative near-exact duplicates по normalized statement + token overlap (≥88%)
  2. **Semantic layer** (Haiku LLM): per-branch semantic dedupe, затем cross-branch semantic dedupe; claims сортируются и идут в overlapping окна (`40` with stride `20`), так что batch-boundary coverage лучше, чем у старых disjoint batches, хотя это всё ещё не exhaustive all-pairs comparison
- semantic dedupe использует strict prompt: мерджатся только near-identical restatements одной пропозиции; problem vs institutional answer, regime vs later transition away, legal rule vs later application/restoration, cause/consequence и general/specific не мерджатся
- semantic dedupe apply (both per-branch and cross-branch) enforces `claim_type` match and numeric/date anchor (`numbers_and_dates`) match, otherwise the merge is skipped with explicit `skip_reason` in debug; cross-branch additionally consolidates overlapping-window suggestions before apply so opposite-direction pairs do not create cycles; debug captures raw suggestions, consolidated groups, applied merges, skipped merges, plus rerun input origin (`pipeline_state_claim_ids` vs `checkpoint_pre_node_state`)
- перед dedupe incoming `state["claim_ids"]` санитизируется against DB: stale/missing IDs не участвуют в merge logic и отдельно попадают в debug summary как `requested/loaded/missing`
- если isolated rerun `dedupe_claims` стартует из checkpoint state и часть claim rows уже была hard-deleted прошлым dedupe pass, node сначала восстанавливает missing rows из `dedupe_source_claims`, а уже потом запускает merge logic
- evidence links и narrative flags мерджатся на keep-клейм до verifier
- `StepExecutionStopped` пробрасывается через все слои (не глотается catch-all)

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
- showrunner prompt агрессивно курирует claims per act, теперь получает `total_claim_count` + явное правило ceiling-per-act / proportional-by-duration, но post-normalization использует этот ceiling только как advisory signal: pathological overload логируется в debug/worker logs, а claims больше не переносятся между актами и не дропаются без участия LLM; disputed/conflict claims по-прежнему валидируются post-hoc и получают debate slot если LLM его пропустил
- planning payloads now carry `output_language`, so showrunner and act planner keep human-readable output in the podcast language instead of drifting into English
- showrunner prompt теперь role-first: LLM должна сначала определить distinct act jobs (`story_role` как тип listener work, а не topic label), затем раскладывать claims; `question_ids` убраны из showrunner payload как provenance-noise, а `branch_ids` оставлены только как слабый anchoring hint после выбора роли акта

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
- prompt act planner теперь также жёстко держит `description` как beat card (`1-3` предложения, максимум `250` символов) вместо mini-draft narration
- prompt act planner теперь жёстче различает `same mechanism in new words` и `genuinely new layer`: later acts должны добавлять stress test / cost / contrast / legal lock-in / consequence, соседние сцены не должны повторно прогонять одну procedural chain под разными ярлыками, historical acts должны по умолчанию оставаться хронологически читаемыми, а callback-heavy endings обязаны начинать beat card с нового move, а не с recap механики
- после normalizer act planner сохраняет явные `primary_claim_ids` / `callback_claim_ids`, если модель вернула их сама; для legacy outputs без этих полей остаётся детерминированный fallback split по порядку сцен. Для callback-heavy beats дополнительно задаётся `scene_job` (`reframe`, `cost`, `contrast`, `bridge`, `synthesize`, `apply`, `stress_test`), чтобы downstream writing видел, какой move сцена должна сделать
- act planner теперь также сокращает LLM-facing `claim_ids` и bundled `evidence_ids` до opaque short labels и восстанавливает их после parse; beat-sheet quality больше не зависит от перепечатки raw UUID
- `hook` / `explanation` / `case_study` сцены не могут остаться callback-only без нового primary claim и в таком случае срабатывает отдельный quality-retry budget (`ACT_PLANNER_QUALITY_RETRIES = 2`); transport/timeout/parse failures используют отдельный request/output retry budget (`ACT_PLANNER_REQUEST_RETRIES = 2`), поэтому сетевой stall не съедает последний шанс на quality retry. Callback-only ending scenes сохраняют structural limits на `<=2` callback claims и ending-friendly `scene_job`; длинный ending description теперь остаётся advisory signal, а не hard fail, потому что length alone слишком brittle across languages/corpora
- quality gate включает два LLM-based чека (Haiku, параллельно): **claim-description grounding** (описание сцены должно следовать из её клеймов, а не пересказывать тезис акта; в проверку уходит полный description без mid-sentence truncation) и **cross-scene insight dedup** (две сцены не должны приземлять один и тот же инсайт). Оба возвращают `severity: structural` и триггерят retry с feedback
- overlap retry feedback теперь не ограничивается общим `rewrite`: planner получает явное указание не перефразировать тот же механизм, а либо слить сцену с более сильной, либо понизить её до callback (`reframe` / `contrast` / `cost` / `bridge` / `synthesize`), либо переписать в genuinely different move (`apply`, `stress_test`, consequence, tradeoff, exception, comparator)
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
  - `previous_act_tail_dialogue` — живой хвост последних 1-2 сцен предыдущего акта (speaker+text), чтобы первая сцена нового акта продолжала тот же разговор, а не ресетилась в более базовый вопрос
  - `prior_scenes_dialogue` — полный диалог всех предыдущих сцен текущего акта (speaker+text), чтобы writer видел, что уже прозвучало и не повторял инсайты
  - `already_covered_claims`
  - `already_covered_claims_global`
  - `transition_note`
  - `scene_claim_mode`
  - `scene_job`
  - `callback_claims` — полные объекты callback-клеймов (id, statement, verification, type), а не голые UUID
  - `recent_scene_starts`

- держит dialogue в `language` подкаста, а не дрейфует в английский или смешанный output
- если evidence/quote/source material на другом языке, writer/polish/repair пересказывают его на языке подкаста вместо raw foreign-language вставки; короткий оригинальный фрагмент допустим только когда сам wording и есть предмет сцены

- использует `description` как main job сцены, а `transition_note` только как лёгкий landing/bridge

- если текущая сцена пересекается с локально или глобально уже покрытым материалом, делает callback и двигается к новому смыслу вместо полного re-explain

- continuity для `previous_scene` и первых сцен новых актов строится от фактически написанного диалога:
  - `previous_scene` несёт `what_listener_now_knows`, реально прозвучавшие `claim_ids` и `last_line` предыдущей сцены
  - `previous_act_summary` по возможности собирается из реально использованных `claim_ids` и последней реплики предыдущего акта, а не только из planning metadata
  - `previous_act_tail_dialogue` собирается из реально написанного хвоста предыдущего акта и даёт writer'у живую seam continuity, а не только compact summary

- в локальный writer prompt попадают только `primary_claim_ids` текущей сцены как `claims`; `callback_claims` (полные объекты), `scene_claim_mode` и `scene_job` нужны как continuity signal

- screenwriter output claim_ids валидируются post-hoc: невалидные UUID (опечатки, hallucinated) дропаются с warning
- `scene_job` даёт callback-heavy сценам положительный контракт:
  - `reframe`
  - `cost`
  - `contrast`
  - `bridge`
  - `synthesize`
  - `apply`
  - `stress_test`

- `recent_scene_starts` — детерминированный список недавних opener-shapes внутри текущего акта; writer использует его как soft anti-repetition cue, чтобы сцены не начинались одним и тем же маркером снова и снова
- planner-to-writer handoff теперь несёт и listener-facing clarity contract:
  - `listener_state_before`
  - `listener_state_after`
  - `must_make_clear`
  - `must_not_assume`
  - `new_terms_to_introduce`
  - `required_bridge_from_previous_scene`
- writer должен использовать эти поля как constraint на понятность сцены, а не как ещё один слой декоративных заметок

- если предыдущая сцена уже сформулировала punchline, новая сцена должна по возможности продвигать разговор дальше, а не перефразировать тот же вывод в первых 1-2 репликах

- writer варьирует discourse shapes абстрактно, а не через словарные blacklist'ы:
  - не каждый challenge начинается как interruptive opener
  - не каждое clarification exchange обязано идти по схеме question -> explicit confirmation -> explanation
  - direct continuation, answer, or correction часто лучше, чем очередное подтверждение-маркер
  - первая сцена нового акта по умолчанию продолжает тот же разговор и не должна уходить в более базовый pedagogical reset, если предыдущий акт уже поднял более сильный hanging question
  - `B asks -> A confirms -> A explains` не считается допустимым default rhythm; B может вносить гипотезу, возражение, наблюдение или последствие без вопросительной формы

- speaker labels в final dialogue нормализуются к ASCII `A/B`, даже если модель вернула кириллические `А/Б`, формы вроде `Host A`, или project-specific host aliases derived from `ShowBible.hosts` (имена / роли ведущих)

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
- получает явный `language` и оценивает spoken naturalness / rhythm в этом языке, а не по implicit English defaults
- сначала аудирует все сцены, затем строит cheap act-level repetition/distribution report и тратит budget на polish **per act**, а не effectively per run
- act-level audit теперь видит **полный текст акта**, а не только scene edges: он получает full scene dialogue, `scene_job`, local issue types, opener/closer counts и turn-pattern context, поэтому repeated opener / confirmation / explanatory-reset families ранжируются по реальному тексту акта, а не по surrogate summary
- отдельно добавлен compact episode seam audit по границам актов (`A1->A2`, `A2->A3`, `A3->A4`), чтобы отличать нормальный handoff от `replay_question` / `replay_punchline` / `weak_reset` на opening scene следующего акта
- scene-level `dialogue_audit` теперь intentionally compact and machine-facing:
  - максимум `3` issues на сцену
  - короткие English `description` / `suggestion`
  - повторяющиеся root causes схлопываются до одного issue перед routing
- audit теперь также смотрит на scene-level comprehension failures, а не только на rhythm/style:
  - `undefined_actor`
  - `unexplained_term`
  - `namedrop_without_grounding`
  - `causal_gap`
  - `missing_bridge`
  - `assumed_context`
  - `summary_instead_of_scene`
  - `compressed_history_dump`
  - `payoff_restarts_explanation`
- эти comprehension issues оцениваются against planning-side `scene_metadata`; они не должны прятаться под generic `teacherly_exposition` или `dry_facts`
- medium-severity проблемы с scope `scene_polish` пытается чинить inline через scene-level polish pass
- prompt для polish получает `scene_job` и `language`, чтобы polish не ломал intended move сцены и не дрейфовал в translated-from-English phrasing
- prompt для polish жёстко держит canonical speaker contract: `speaker` должен оставаться только `A/B`; если модель всё же возвращает имена ведущих или role labels, runtime normalization мапит их обратно через aliases из `ShowBible.hosts`
- prompt для polish может получать compact `act_repetition_report`, `seam_finding` и `intervention_targets`, чтобы сцена чинила конкретный opener / seam / distribution defect, а не делала generic style rewrite
- `dialogue_polish` payload теперь также несёт `intervention_mode` и `success_criteria`, so repair prompt starts from the concrete target (`opener_repair`, `seam_repair`, `agreement_shape_repair`, `rhythm_repair`) before doing any generic cleanup
- между single-scene polish и full `screenwriter` rewrite добавлен pilot `multi_scene_polish`: если act audit видит contiguous `repeated_explanatory_reset` cluster, Phase 6 может переписать соседний блок сцен одним проходом, не притворяясь, что это три независимых local bugs
- fresh `write_screenplay` draft resets downstream phase-6 revision state for that scene (`revision_count`, pending audit state, stale findings), so rerun means a new screenplay draft with a new polish budget
- act-level distribution findings now also become routing signals:
  - `llm_priority_scene_numbers` remain the main intervention points
  - non-priority scenes with only act-level duplicate-family pressure no longer auto-spend polish budget
  - `quality_signals.dialogue_act_monotony_signals` stores repeated-family summaries for downstream inspection
- `quality_signals` now also carry structural rhythm telemetry (`speaker_question_share`, dominant opener-speaker share, opener-shape diversity, `B question -> A` opening rate), so repeated-word counts are no longer the main repetition diagnostic
- `quality_signals.dialogue_timing` теперь также хранит rough runtime estimate (`word_count`, `estimated_seconds_150/145/130_wpm`, `target_seconds`, `delta_seconds_vs_target_145_wpm`, `below_target_warning`)
- если polish правит opening scene later act-а с seam-targetом, phase 6 делает targeted post-polish seam re-check только для этой границы и использует обновлённый seam result как final status
- acceptance for `scene_opening_echo` / `boundary_restatement` is now target-aware: polish не принимается, если opener по сути остался тем же; для speaker-distribution intervention polish тоже reject, если opener остался в том же dominant-speaker skew
- metadata safety is now contract-first: `dialogue_polish` должен возвращать compatible `emotion` / `ssml_hints`; same-length rewrites get safe metadata backfill, while changed-length rewrites are accepted only when the returned dialogue itself preserves usable delivery metadata
- `dialogue_polish` shortens LLM-facing `claim_ids` before the Sonnet call and restores them after parse, so polish can preserve claim coverage without retyping long UUIDs
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
- detection-pass и repair-пассы получают явный `language`, чтобы factual / conversational judgments делались в языке диалога, а не по implicit English conventions
- detection-pass теперь может возвращать structured issue signals вроде `line_function` / `action_required`, чтобы post-filter различал open questions, premise-heavy questions, recaps и transitions без system-level phrase dictionaries
- эти structured signals теперь участвуют и в runtime semantics: non-question transitions / synthesis lines can be downgraded from actionable findings, and `action_required=false` is honored in `passed`, repair-trigger, and blocking logic instead of being ignored after normalization

- detection-pass получает не весь episode claim universe, а scene-local claim subset: `dialogue.claim_ids` + manifest-assigned scene claims

- `missing_claim_link` не должен срабатывать на чисто разговорных / риторических / synthesis линиях, если они не вводят новый factual assertion

- medium/high severity issues запускают scene-level `citation_repair` pass

- `citation_repair` теперь ориентирован не только на rigor, но и на spoken-script quality:
  - prompt explicitly keeps podcast voice instead of drifting into legalese / academic prose
  - repair strategy order is `reuse existing claim link -> local hedge/narrowing -> softer nearby wording -> deletion only as last resort`
  - scene context now carries `is_final_scene_in_act` / `is_final_scene_in_episode`, so mid-episode bridges should not mutate into fake sign-offs like `об этом в следующий раз`
  - scene-level repair gets flagged-line payloads and is told to focus edits on flagged lines while keeping clean lines as intact as possible
  - prompts preserve named actors / agency and include contrastive examples against teaser-language, double hedging, and abstract role-speak
  - mechanism-heavy explanation lines now explicitly preserve supported mechanism steps and narrow only the unsupported fragment, while quote-like or source-like framing must be removed unless claim support actually carries that attribution frame

- audit-pass имеет bounded retry на transient timeout/parse failures; если все попытки исчерпаны, сцена fail-closed превращается в blocking `audit_failure`

- после LLM-аудита есть cheap post-filter для неутверждающих вопросительных реплик: only a tiny whitelist of genuinely generic questions (`Что произошло?`, `Что дальше?`) downgrade to `record`; recap / confirmation questions and loaded wh-questions with embedded factual premises stay actionable instead of fail-open. `unverified_stated_as_fact` on a pure generic question no longer triggers pointless `line_repair`

- repaired scene повторно аудируется на том же narrowed context; repair принимается по blocker-focused rules:
  - сначала используется `line_repair`: модель переписывает только flagged line indexes, а auditor делает mechanical splice и сохраняет все unflagged lines без изменений
  - `scene_repair` используется только как fallback, если проблема шире локального patch
  - line-role sensitivity: opener / landing / penultimate lines считаются sensitive, поэтому citation layer предпочитает `line_repair` на таких линиях и не даёт `scene_repair` свободно переписывать unflagged handoff lines
  - accepted `scene_repair` теперь metadata-aware: если repaired line текстуально совпадает с original, сохраняются `emotion` / `ssml_hints`; если меняются только `claim_ids`, line-level audio cues тоже сохраняются
  - original blocking issues must be resolved/downgraded
  - no new blocking issues
  - required claim coverage must be preserved for assertive content; only repaired question lines may drop stale `claim_ids`
  - for non-blocking repairs, acceptance now biases toward changed/relevant lines instead of demanding full-scene re-audit stability, so unrelated new medium noise on untouched lines does not automatically reject a locally improved repair
  - line-count drift must stay bounded for scene-level repairs (no free-form scene rewrite disguised as rigor repair)

- `available_claims` now carry attribution/source-support metadata, and citation issues may include `unsupported_source_framing` when a line starts sounding explicitly source-attributed without underlying claim support for that rhetorical move
- citation repair shortens LLM-facing `available_claims` keys and dialogue `claim_ids` before repair, then restores them after parse; reuse of support links no longer depends on reproducing raw UUIDs

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
- `GET /api/generations/{run_id}/artifacts` — вернуть persisted артефакты текущего run (`episode plan`, `beat sheets`, screenplay) для step-mode UI.
- `POST /api/generations/{run_id}/steps/next` — выполнить следующий node.
- `POST /api/generations/{run_id}/steps/rerun` — переисполнить выбранный уже выполненный node; если `target_node` не передан, берется последний выполненный шаг.
- `POST /api/generations/{run_id}/steps/stop` — остановить текущий/очередной step action.
- `rerun` доступен только после как минимум одного успешно завершенного шага (нужен checkpoint + `last_node`).
- после каждого шага и после подтвержденной остановки публикуется realtime-событие `generation_paused` с `executed_node` и `next_node` (дополнительно может содержать `stopped=true` и `reason`).
- step-mode UI показывает именно persisted содержимое pipeline, а не debug dumps: `EpisodeManifest.acts_json` для `episode plan` / `beat sheets` и `ScriptSegment.dialogue_json` для текущего screenplay.
- показ артефактов тоже привязан к `last_node`: если rerun ушёл назад до `write_screenplay`, UI скрывает stale screenplay, пока phase 6 не будет выполнена заново.

Назначение: отладка и итеративная проверка фаз без стоимости полного прогона.

---

## **Сигналы качества (простые метрики)**

- % factual lines covered by Claim IDs

- доля VERIFIED среди ключевых FACT claims

- количество DISPUTED, которые проговорены (а не замолчаны)

- плотность narrative assets (CASE/ANECDOTE/QUOTE) по секциям

- соответствие audience_level (термины объяснены/не пережёваны)
