# Универсальный pipeline: полный список предложений в LLM-first рамке

## Зачем нужен этот документ

Этот документ собирает все мои актуальные предложения по развитию pipeline в одной рамке:

- pipeline должен быть универсальным и работать на любых темах
- semantic problems нужно в первую очередь решать через `prompt`, `payload`, `question decomposition` и `LLM policy`
- deterministic logic нужна не как замена нормального prompt design, а только там, где она отвечает за:
  - data integrity
  - identity
  - dedupe
  - provenance correctness
  - cheap objective fixes

Главный принцип:

- сначала улучшаем то, как LLM понимает задачу
- потом добавляем минимально необходимый deterministic слой

Иными словами:

- не "обложить всё проверками"
- а "дать модели лучший контекст, лучшую постановку задачи и более правильную структуру данных"

---

## 1. Базовые принципы

### 1.1. Universal, not case-specific

Нельзя проектировать pipeline под конкретную тему, жанр или тип источников.

Все решения должны быть сформулированы так, чтобы работать для:

- истории
- политики
- экономики
- науки
- технологий
- культуры
- конфликтных тем
- explainers
- narrative episodes

Плохая формулировка:

- "для темы про Гонконг нужен прямой источник про Гонконг"

Хорошая универсальная формулировка:

- для вопросов определённого типа pipeline должен различать `direct answer evidence` и `contextual evidence`

### 1.2. LLM-first

Semantic quality прежде всего определяется:

- правильной декомпозицией вопросов
- качеством retrieval payload
- качеством prompt contract
- качеством examples
- тем, насколько downstream получает не плоский список кусков текста, а структурированный research context

Поэтому первыми должны идти улучшения:

- decomposition
- retrieval prompts
- claim-builder prompts
- verifier prompts
- planning payload

### 1.3. Deterministic-only where necessary

Deterministic logic нужна только там, где LLM не должна решать проблему:

- canonical identity
- exact duplicates
- provenance pairing
- unique constraints
- merge semantics
- source reuse

То есть:

- `direct vs contextual` — это LLM/semantic layer
- `source_id vs canonical_source_id` — это deterministic layer

---

## 2. Что сначала исправлять через prompt / context / decomposition

Ниже список проблем, которые в первую очередь надо лечить не проверками, а LLM-слоем.

### 2.1. Начать использовать существующий `intent`

`intent` уже есть в decomposition contract:

- `history`
- `explain`
- `compare`
- `debate`
- `case_study`

Сейчас главный gap не в том, что intent-типов мало, а в том, что downstream их не использует.

Зачем:

- разные intent-ы требуют разного evidence
- для `history` timeline и case работают иначе, чем для `compare`
- для `debate` и `case_study` полезность illustrative/context evidence выше, чем для чистого `explain`

Это должен знать:

- Hunter
- Librarian
- Claim Builder
- Planner

Важно:

- не надо сейчас расширять таксономию до 10 типов
- сначала нужно передать и реально использовать уже существующие 5

### 2.2. Разделить direct answer evidence и contextual evidence

Это одна из самых важных semantic задач.

Pipeline должен различать:

- `direct evidence`
  - прямо отвечает на question
- `contextual evidence`
  - даёт фон, аналогию, смежный механизм, исторический прецедент, narrative framing

Это не должно быть жёстким deterministic классификатором.
Это должен определять LLM на основе:

- текста вопроса
- `intent`
- цитаты
- source metadata
- branch context

Зачем:

- coverage по asset types сама по себе не гарантирует, что вопрос реально закрыт
- contextual evidence полезно для подкаста, но не должно маскироваться под прямой ответ

Важно:

- это question-level, а не evidence-level сигнал
- один и тот же canonical evidence может быть `direct` для одного question и `contextual` для другого
- поэтому это distinction должно жить в `EvidenceProvenance` или эквивалентном retrieval-context layer, а не в `EvidenceSpan`

### 2.3. Использовать текущий decomposition contract downstream

Текущий decomposition уже даёт полезный контракт:

- `question`
- `intent`
- `required_evidence`
- `expected_evidence`
- `quality_need`

Зачем:

- этого уже достаточно, чтобы заметно улучшить Hunter / Librarian / Claim Builder prompts
- не нужно сначала добавлять новые structural fields вроде `answer_shape`
- не нужно добавлять `narrative_role` как отдельное поле в схему

### 2.4. Улучшить Hunter prompt

Hunter должен получать richer payload и richer extraction contract.

В prompt Hunter-а стоит передавать:

- `question_text`
- `intent`
- `required_evidence`
- `expected_evidence`
- краткий branch context
- примеры того, что считается direct evidence и contextual evidence для этого типа вопроса

Что нужно требовать от Hunter:

- не просто извлечь quote
- а оценить, является ли evidence:
  - direct answer
  - contextual background

Результат этой оценки должен записываться не на `EvidenceSpan`, а в provenance/context layer для конкретного question.

Также стоит просить модель:

- не переизвлекать одинаковые куски ради разных вопросов без новой смысловой функции
- не тащить broad explainer как answer на всё подряд

### 2.5. Улучшить Librarian / fast web prompt

Fast web должен не просто закрывать missing asset types, а получать richer question context.

Нужно передавать:

- `intent`
- `required_evidence`
- `expected_evidence`
- richer `missing_assets`
- краткий branch context

Семантический `gap_reason` — хорошая идея, но это не free field.
Он сам требует отдельной генерации.

Поэтому:

- не стоит делать его P0
- сначала нужно использовать уже имеющийся decomposition contract
- semantic `gap_reason` можно считать P2 / later improvement

### 2.6. Улучшить Claim Builder prompt

Claim Builder должен строить claims не из "списка evidence", а из research-aware payload.

В payload стоит передавать:

- `intent`
- `branch context`
- source metadata
- provenance summary
- contextual asset types
- direct/context labels по evidence, если они появились выше

Что нужно явно требовать от модели:

- отличать factual support от contextual background
- не делать claim только потому, что кусок текста выглядит содержательно
- использовать illustrative evidence осмысленно, а не случайно
- не делать semantic paraphrase duplicates
- не смешивать разные question intents в один claim

### 2.7. Улучшить verifier prompt

Verifier должен проверять не только "поддерживает ли evidence claim", но и:

- claim действительно выражает то, что утверждается в evidence
- support direct или only contextual
- насколько source primary/secondary/tertiary
- насколько claim опирается на одну explanation surface vs multiple independent support paths

Verifier лучше использовать как semantic judge, а не как место, где через rules пытаются компенсировать слабый claim-builder.

### 2.8. Улучшить planning payload

Planner / writer не должны получать просто "набор claims".

Им нужно передавать:

- verification level
- direct/context signal
- `illustrative` из `ClaimEvidence`
- source diversity hints
- branch/question intent

Тогда planner сможет различать:

- надёжное factual ядро
- background
- narrative framing

Без этого planning слишком зависит от случайной плотности claims.

---

## 3. Какие universal semantic сигналы нужно добавить

Это не hard checks и не блокирующие правила. Это сигналы, которые нужны модели и аналитике.

### 3.1. Answer adequacy

Нужно считать не только `coverage`, но и `answer_quality`.

Разница:

- `coverage` отвечает на вопрос: нашли ли мы evidence нужных типов
- `answer_quality` отвечает на вопрос: реально ли найденный evidence отвечает на research question

Минимально полезные сигналы:

- `direct_evidence_count`
- `contextual_evidence_count`
- `answer_quality_score`

### 3.2. Source concentration

Source concentration не должна быть hard stop по умолчанию.

Но это важный quality signal:

- доля support-links от одного source
- доля claims, опирающихся only on one source
- доля claims, опирающихся only on unknown/secondary sources
- concentration внутри branch/question

Это должно быть:

- metric
- debug signal
- observability layer

А не автоматический reject без необходимости.

### 3.3. Narrative retention

Для подкаста важно мерить, не теряется ли narrative material.

Полезные метрики:

- сколько `case/anecdote/expert_quote` вошло в evidence pool
- сколько из них дошло до claims
- сколько из них стало `illustrative`
- сколько narrative evidence было использовано как sole factual support

Это не столько policy-gate, сколько способ видеть, становится ли output сухим.

### 3.4. Direct/context balance

Для каждой ветки полезно понимать:

- сколько direct support
- сколько contextual support
- сколько illustrative evidence

Это позволит видеть тип перекоса:

- ветка хорошо объяснена, но плохо доказана
- ветка доказана, но не пригодна для storytelling
- ветка полна background, но не отвечает на вопрос

---

## 4. Deterministic база, которая уже нужна и в основном уже реализована

Ниже список вещей, которые не стоит отдавать на волю модели.
Это не столько новый roadmap, сколько baseline-принципы, которые должны оставаться в системе.

### 4.1. Canonical source identity

Нужен canonical source identity:

- `source.id`, если source canonical
- `source.is_duplicate_of`, если source duplicate

Это must-have.
Иначе система путает:

- несколько копий одного источника
- независимое подтверждение

### 4.2. Canonical evidence identity

Нужен exact evidence dedupe:

- canonical source
- normalized quote
- normalized locator

Это data integrity, а не semantic judgment.

### 4.3. Provenance correctness

Нельзя терять реальные provenance pairs.

Система должна сохранять:

- какой evidence найден для какого question
- в каком branch
- в каком run
- каким retriever-ом

И нельзя recombine-ить это потом synthetic Cartesian product.

### 4.4. Reuse existing source вместо создания blind duplicates

Если canonical web source уже есть, pipeline не должен плодить новые temp rows для того же URL.

Это не semantic проблема.
Это просто корректность и cost control.

### 4.5. Conservative deterministic dedupe claims

После локальной claim generation нужен deterministic dedupe:

- exact duplicates
- conservative near-exact duplicates

Это оправдано, потому что одна и та же semantic единица может вылезти из разных батчей.

Но dedupe должен быть консервативным.
Нельзя превращать его в loose semantic rewrite engine.

### 4.6. Unique constraints и merge semantics

Это тоже не задача LLM.

Нужно deterministically гарантировать:

- уникальность provenance links
- корректный merge claim-evidence relations
- отсутствие взаимоисключающих флагов в одной связи

---

## 5. Где именно не стоит начинать с hard checks

Ниже список вещей, которые не стоит в первую очередь решать через жёсткие правила.

### 5.1. Source concentration

Не надо сразу делать:

- "если один source > X%, то reject"

Сначала это должен быть signal.

Почему:

- для узкой темы один сильный primary source может быть нормальным
- для explainers может быть нормально, что один source даёт много атомарных facts

То есть сначала:

- metric
- debug visibility
- prompt pressure

И только потом, если нужно, soft policy.

### 5.2. Direct/context separation

Не стоит пытаться делать это regex-ами и if-ами.

Это semantic distinction.
Она зависит от:

- question intent
- branch context
- type of claim
- type of source

### 5.3. "Хорошо ли question закрыт"

Это тоже не нужно сводить к:

- required_assets matched = question solved

Нужна LLM-level adequacy assessment.

### 5.4. "Подходит ли source для этого claim"

Source suitability — не только вопрос reliability/authority по домену.

Здесь нужен LLM judgment:

- source primary или background для конкретного claim
- quote действительно подтверждает claim или только тематически связан

---

## 6. Приоритеты изменений

Если расставлять приоритеты в LLM-first порядке, я бы делал так.

### 6.1. P0: Передать существующий `intent` downstream

Нужно не расширять taxonomy, а начать использовать то, что уже есть, в:

- Hunter
- Librarian
- Claim Builder

### 6.2. P0: Передать `ClaimEvidence.illustrative` в planner

Это дешёвый и высокоимпактный win.

### 6.3. P0: `authority_type` должен реально наполняться

Без этого verifier/source-quality semantics остаются мёртвыми.

### 6.4. P1: direct/context distinction как provenance-level сигнал

Это важный semantic шаг, но он требует явного архитектурного решения:

- это question-level signal
- значит его место в provenance/context layer, а не в canonical evidence

### 6.5. P1: Better claim-builder / verifier semantics

Нужно дать моделям richer payload:

- intent
- source metadata
- provenance summary
- contextual asset types
- direct/context labels, если они уже появились

### 6.6. P2: Semantic `gap_reason`

Это полезно, но это уже отдельный semantic eval step.

### 6.7. P2: Расширение intent taxonomy

Не нужно делать это до того, как текущие 5 intent-типов вообще начнут работать downstream.

---

## 7. Что надо улучшать в data layer прямо сейчас

Это уже не про semantic taste, а про структуру.

### 7.1. `authority_type` должен реально наполняться

`authority_type` должен жить на `Source`.

Fill path:

- preliminary heuristic classification при создании temp source
- final classification в canonicalization / ingestion
- при необходимости LLM fallback по `title + preview + url + source_type`

Важно:

- `authority_type` должен быть source-level signal
- но не должен автоматически доказывать truth of claim

### 7.2. Provenance должен оставаться first-class layer

Нужен нормальный `EvidenceProvenance`:

- `evidence_id`
- `generation_run_id`
- `question_id`
- `branch_id`
- `asset_type`
- `direct_or_contextual`
- `retrieved_by`
- `query_text`
- `retrieval_score`

Важно:

- `direct_or_contextual` сейчас отсутствует в текущей схеме и потребует отдельного schema-change
- это не P0 field migration "на всякий случай"
- сначала Hunter/Librarian должны научиться стабильно производить этот сигнал
- только после этого имеет смысл добавлять его в модель и миграцию

### 7.3. Context-specific semantics должны жить в provenance

Один canonical evidence может играть разные роли в разных branch/question contexts.

Поэтому:

- canonical evidence не должен быть единственным носителем semantic role
- context-specific role должен жить в provenance or equivalent context layer

### 7.4. Debug dumps должны лучше отражать branch/question context

Для анализа полезно явно писать в dumps:

- batch branch grouping
- direct/context labels
- gap reasons
- evidence role summary

Иначе приходится реконструировать смысл постфактум.

---

## 8. Observability backlog, не immediate pipeline work

Это не hard rejects и не текущий pipeline roadmap.
Это отдельный observability/analytics backlog.

### 8.1. Retrieval quality

- vector hit count
- keyword hit count
- keyword hit distribution by branch
- saved evidence per question
- direct/context split per question

### 8.2. Evidence quality

- unique evidence count
- exact duplicate pressure
- source concentration
- reliability distribution
- authority distribution
- direct/context distribution

### 8.3. Claim quality

- claims per branch
- exact duplicate statements
- support-links per claim
- illustrative usage rate
- claims by claim type
- single-source only rate
- unknown-only support rate

### 8.4. Downstream usefulness

- percentage of branches with enough direct support
- percentage of branches with narrative hooks
- percentage of claims surviving dedupe
- percentage of claims later upgraded by verifier

---

## 9. Чего НЕ нужно делать

Чтобы не скатиться в over-engineering, я бы не делал следующее.

### 9.1. Не пытаться всё решить правилами

Если проблема semantic, её нельзя качественно решить одними if/else.

### 9.2. Не превращать quality signals в жёсткие блокеры слишком рано

Сначала:

- метрика
- анализ
- prompt improvement

Только потом:

- soft policy
- и лишь в крайнем случае hard gate

### 9.3. Не подменять decomposition validation-слоем

Если дерево вопросов плохое, verifier не должен становиться костылём вместо нормального decomposition.

### 9.4. Не делать prompt layer слишком бедным

Много проблем downstream возникают не потому, что LLM "не справляется", а потому что ей дали:

- плохой task framing
- мало контекста
- слишком плоский payload

---

## 10. Короткая итоговая формула

Если сжать всё до одного принципа, то он такой:

- semantic quality нужно повышать через `better decomposition + better prompts + better payloads`
- deterministic logic нужна только для `identity + provenance + dedupe + structural correctness`

Правильный порядок работы:

1. Начать использовать уже существующий `intent`.
2. Улучшить Hunter / Librarian / Claim Builder prompts и payloads.
3. Передать `illustrative` и richer context в planner.
4. Улучшить verifier как semantic judge.
5. Держать deterministic слой узким и строгим:
   - canonical identity
   - provenance correctness
   - exact dedupe
   - conservative claim merge

Это и есть та рамка, в которой pipeline остаётся:

- универсальным
- расширяемым
- не переобложенным костылями
- и при этом достаточно надёжным
