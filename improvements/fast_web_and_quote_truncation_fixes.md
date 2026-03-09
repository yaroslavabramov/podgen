# Fast Web And Quote Truncation Fixes

## Контекст

Последний прогон `run-27eeb6a3-6a47-412b-81da-12cadf902054` показал две системные проблемы:

1. `research_fast_web` покрывает слишком маленькую часть verifier gaps.
2. `claim_builder` и особенно `verifier` сами режут quotes так сильно, что создают часть residual gaps искусственно.

Это не проблемы темы про HKD. Это универсальные проблемы pipeline.

---

## 1. Что не так с Fast Web

### Текущее поведение

В [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py) `_select_gap_branches()` жёстко ограничивает verifier gaps первыми тремя:

- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L272)

```py
return normalized[:3]
```

При этом сами search queries строятся почти напрямую из `question`:

- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L299)
- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L307)

А `named_tokens` вытаскиваются только regex-паттерном для ASCII:

```py
re.findall(r"[A-Za-z0-9$.-]{2,}", question)
```

Это значит:

- основной query уходит в web search в языке вопроса
- для кириллицы lexical expansion почти не происходит
- `targeted query` часто отличается от исходного вопроса только датой или аббревиатурой вроде `HKMA`

Отдельно есть ещё одна проблема: governor объявлен как `max web sources per act`, но фактически считает не источники, а созданные evidence items:

- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L67)
- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L100)
- [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L179)

### Почему это плохо

Это старый coarse throttle, который появился как дешёвый способ ограничить latency/cost. Но сейчас в pipeline уже есть другие governor'ы:

- `max_fast_web_per_act`
- максимум 2 web pages на gap
- максимум 3 extracted evidence items per question

Поэтому `[:3]` сейчас стал не защитой, а лишним bottleneck.

Кроме этого:

- поиск по web не должен быть привязан к языку подкаста или вопроса
- для не-английских вопросов текущие queries плохо находят high-signal global sources
- ASCII-only token extraction не помогает кириллическим вопросам
- governor семантически запутан:
  - комментарий и имя говорят про `sources`
  - фактически лимит применяется к `evidence items`
  - значит один продуктивный gap может полностью съесть budget и лишить последующие gaps шанса на поиск

### Что показал последний прогон

По БД у этого run было:

- `11` verifier gaps после первого verifier-pass
- fast-web добавил только `5` provenance rows
- они покрыли только:
  - `b1`: 1
  - `b1.1`: 3
  - `b3`: 1

То есть fast-web вообще не дошёл до `b4`, `b5`, `b6`, хотя gaps там были.

По самим добавленным evidence видно, что retrieval quality была слабой:

- 3 evidence пришли из русской Википедии про серебряный стандарт
- 2 evidence пришли с `monetnik.ru`

То есть loop почти не нашёл новые сильные corroborating sources и в основном добавил ещё немного фонового материала.

### Что стоит поменять

#### Обязательно

- Убирать `normalized[:3]` только вместе с починкой governor semantics.
- После этого итерироваться по всем verifier gaps до исчерпания нормального budget, а не до hardcoded count.
- Развязать язык поиска и язык вопроса:
  - не использовать `question` как единственный search query
  - строить `language-aware` query variants для web retrieval
  - для неанглоязычных вопросов делать явный LLM-based rewrite в английский retrieval query
- Перестать использовать ASCII-only expansion как основную механику query enrichment.
- Исправить governor semantics:
  - основной governor должен считать `processed gaps`
  - а не `evidence items`
  - `unique URLs / sources` и `max extracted evidence per gap` могут оставаться вторичными локальными ограничителями
  - сейчас поведение и название расходятся

#### Желательно

- Сортировать gaps не только по `priority`, но и по severity сигнала из verifier, когда он появится.
- Если budget заканчивается, прекращать loop по governor, а не по hardcoded count.
- Для каждого gap делать не один сырой вопрос, а небольшой набор query variants:
  - original question
  - retrieval-language rewrite
  - explicit English rewrite for global web search
  - entity-aware factual query
  - official-source-oriented query, если вопрос явно просит mechanism / rule / legal basis
- Пересмотреть `max_tokens=2048` в web evidence extraction call, если увеличивается число query variants, pages или плотность extractor payload.

### Целевой принцип

Fast web должен ограничиваться **бюджетом**, а не случайным числом `3`, и искать в **языке retrieval**, а не автоматически в языке подкаста.

---

## 2. Что не так с Truncated Quotes

### Текущее поведение

#### Claim Builder

В [claim_builder.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/claim_builder.py#L307):

```py
"verbatim_quote": evidence.verbatim_quote[:700],
```

#### Verifier

В [verifier.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L174):

```py
"quote": ev.verbatim_quote[:300],
```

### В чём логика этих лимитов

#### `claim_builder[:700]`

У этого есть понятная логика:

- branch-batches могут быть большими
- в builder есть явный char budget:
  - [claim_builder.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/claim_builder.py#L29)
  - [claim_builder.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/claim_builder.py#L426)

То есть `700` — грубый budget control.

Но важный нюанс: текущий batch budget считается уже по укороченному `item["verbatim_quote"]`.

Это значит:

- нельзя просто убрать `[:700]`
- сначала нужно перенести budget control на реальные quote lengths
- или переписать packing так, чтобы batching происходил до truncation, а не после неё

#### `verifier[:300]`

Здесь логика уже слабая:

- verifier именно и должен оценивать, достаточно ли quote подтверждает claim
- но ему передаётся только начало quote
- после этого verifier сам же говорит, что quote truncated

То есть часть false negatives создаём мы сами.

### Что показал последний прогон

Во втором verifier-pass `19` claims имели `needs_research=true`.

Из них:

- `12` выглядели как `truncated_or_missing_detail`
- `5` выглядели как `single_source_or_weak`

Проверка по БД показала, что у многих проблемных claims supporting quotes в базе были длиннее, чем payload verifier:

- `360`
- `412`
- `463`
- `624`
- `762`

Но verifier видел только первые `300` символов.

Это касается, например:

- sterling / gold-standard claims
- dual-system claim
- HKNPL governance claim
- banknote denomination/color claims

### Что стоит поменять

#### Verifier: обязательно

- Убрать жёсткий `[:300]`.
- Передавать verifier полный `verbatim_quote`, пока не появится реальный token-aware packing.

Если нужен лимит, он должен быть:

- заметно выше
- и управляться budget-aware batching, а не тупым fixed prefix

#### Claim Builder: желательно

- Не убирать `[:700]` вслепую.
- Сначала пересчитать batching на основе реальной длины quotes, а не уже усечённого payload.
- После этого выбрать один из вариантов:
  - передавать полный quote и полагаться на честный branch-batch budget
  - либо делать adaptive truncation
- Если используется adaptive truncation:
  - если quote короткий, передавать целиком
  - если длинный, передавать окно, которое сохраняет конец и середину, а не только prefix

### Целевой принцип

Нельзя, чтобы verifier массово видел не тот quote, на котором реально построен claim.

---

## 3. Приоритет изменений

### P0

- Атомарно починить fast-web gap selection и governor semantics:
  - убрать `normalized[:3]`
  - одновременно исправить governor так, чтобы budget считался по `processed gaps`
- Перестроить fast-web query generation в `language-aware` variants вместо одного сырого вопроса.
- Добавить явный LLM-based EN rewrite для неанглоязычных вопросов.
- Убрать `ev.verbatim_quote[:300]` из verifier payload.

### P1

- Пересчитать claim-builder batching на реальных quote lengths.
- После этого пересмотреть `evidence.verbatim_quote[:700]` в claim builder.
- Перейти от fixed prefix truncation к budget-aware packing.

---

## 4. Ожидаемый эффект

Если сделать только эти правки, pipeline должен:

- реально пытаться закрывать весь хвост verifier gaps, а не случайные первые 3
- перестать создавать искусственные `truncated` gaps внутри verifier
- уменьшить долю false `needs_research`
- лучше использовать уже найденный материал, а не гонять лишний research loop
