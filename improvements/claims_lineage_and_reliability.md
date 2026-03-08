# Claims / Evidence: целевая архитектура lineage, verification и batching

## Контекст

Этот документ фиксирует не временные обходы, а целевое решение для связки:

- `Evidence`
- `Claims`
- `Verifier`
- `Planning`

Основание:

- логи `tmp/debug/claims/*`
- логи `tmp/debug/hunter/*`
- текущая реализация:
  - `backend/app/pipeline/phase3_research/hunter.py`
  - `backend/app/pipeline/phase3_research/librarian_fast.py`
  - `backend/app/pipeline/phase3_research/tools.py`
  - `backend/app/pipeline/phase4_claims/claim_builder.py`
  - `backend/app/pipeline/phase4_claims/verifier.py`
  - `backend/app/ingestion/canonicalizer.py`
  - `backend/app/models/source.py`
  - `backend/app/models/evidence.py`
  - `backend/app/models/claim.py`

Главный вывод по run: evidence-пул в целом сильный, но pipeline теряет исследовательский контекст и раздувает ложную уверенность на переходе `Evidence -> Claims -> Verification`.

Проблемы текущей реализации:

- `EvidenceSpan` не связан с research tree через нормальную many-to-many provenance-модель
- одинаковые quotes живут как независимые evidence rows
- `claim_builder` работает по плоскому списку evidence и режет его случайными батчами по 24 строки
- narrative evidence почти не сохраняется как narrative
- `verifier` считает независимые источники по сырым `source_id`, а не по canonical source identity
- `verifier` не реализует правило про temporary sources
- `verifier` не создаёт новые research gaps и гоняет loop по старым gap’ам Hunter

## Финальные принципы

1. `EvidenceSpan` — это canonical evidence, а не "владелец вопроса".
2. Связь "где, кем и для какого вопроса evidence найден" хранится в отдельной таблице provenance-links.
3. Source-level provenance и evidence-level provenance не смешиваются.
4. Батчинг строится по provenance-группам, а не по порядку строк в массиве.
5. Дедупликация нужна на двух уровнях:
   - exact-dedupe evidence
   - global dedupe claims после локальной генерации
6. `authority_type` — это source-level signal, а не автоматическое доказательство истинности claim.
7. Narrative preservation решается prompt/policy-слоем и relation-моделью claim-evidence, а не отдельным "магическим" полем на evidence.

## Целевая модель данных

## 1. Source

`Source` остаётся носителем source-level provenance:

- `original_url`
- `source_type`
- `title`
- `published_at`
- `domain_reliability`
- `is_temporary`
- `is_duplicate_of`

Нужно добавить:

- `authority_type: primary | secondary | tertiary | unknown`

`authority_type` не должен оставаться "мёртвым" полем.
Его должен заполнять ingestion/canonicalization pipeline.

Назначение `authority_type`:

- `primary`
  - закон
  - официальный регулятор
  - первичный статистический владелец
  - оригинальная research publication
- `secondary`
  - аналитическая статья
  - новость
  - explainer
  - обзор
- `tertiary`
  - энциклопедический пересказ
  - агрегатор
  - компиляция вторичных материалов
- `unknown`
  - всё, что не классифицировано явно

Важно:

- `domain_reliability` отвечает на вопрос "насколько в целом доверяем домену"
- `authority_type` отвечает на вопрос "насколько этот документ первичен по отношению к фактам"

Это разные сигналы.

`authority_type` нельзя трактовать как автоматическое доказательство того, что любой claim из этого source должен становиться `VERIFIED`.

### Как заполнять `authority_type`

Целевое решение должно быть гибридным, а не только regex-only и не только LLM-only:

1. Сначала deterministic classification для очевидных случаев:
   - regulator / government / standards body / journal / DOI owner -> `primary`
   - major news / explainer / analysis -> `secondary`
   - encyclopedia / aggregator / reference compilation -> `tertiary`
2. Для неоднозначных источников запускать LLM classification на базе:
   - `source_type`
   - canonical URL / hostname
   - `title`
   - `text_preview`
3. `unknown` оставлять только тогда, когда ни rule-based, ни LLM-классификация не дали уверенного результата.

`authority_type` должен выставляться в том же ingestion/canonicalization проходе, где сейчас выставляется `domain_reliability`.

## 2. EvidenceSpan

`EvidenceSpan` должен оставаться самостоятельным canonical evidence:

- `source_id`
- `chunk_id`
- `verbatim_quote`
- `locator`
- `asset_type`
- `reliability_hint`
- `narrative_value_score`
- `tellability_notes`

Он не должен хранить один `question_id` или один `branch_id`.

Причина:

- один и тот же quote может закрывать несколько вопросов
- один и тот же quote может использоваться в нескольких ветках
- один и тот же quote может быть найден повторно в другом run
- один и тот же quote может прийти и из `hunter`, и из `librarian_fast`

## 3. EvidenceProvenance

Нужна отдельная таблица `EvidenceProvenance`.

Она хранит retrieval/evaluation context для canonical evidence.

Минимальный состав полей:

```json
{
  "id": "uuid",
  "evidence_id": "uuid",
  "generation_run_id": "uuid",
  "manifest_id": "uuid",
  "question_id": "string",
  "branch_id": "string",
  "asset_type": "statistic|anecdote|expert_quote|definition|mechanism|case|timeline|contrast",
  "retrieved_by": "hunter|librarian_fast|deep_ingestion",
  "query_text": "string|null",
  "retrieval_score": 0.0,
  "created_at": "timestamp"
}
```

Обязательные свойства модели:

- many-to-many между `EvidenceSpan` и research questions
- один evidence может иметь много provenance rows
- одна question может ссылаться на много evidence
- `question_id` и `branch_id` должны быть `NOT NULL`
- `asset_type` принадлежит provenance-контексту, а не canonical evidence как единственной истине
- уникальный ключ должен запрещать одинаковый provenance-link дважды в одном run

Рекомендуемая уникальность:

- `(evidence_id, generation_run_id, question_id, branch_id, retrieved_by, asset_type)`

Рекомендуемые индексы:

- `(generation_run_id, branch_id)`
- `(generation_run_id, question_id)`
- `(evidence_id)`

## 4. ClaimEvidence

Текущая модель `ClaimEvidence` с одним `relation` недостаточна.

Проблема:

- один evidence может быть одновременно factual support и narrative illustration
- один evidence может быть counter-evidence для claim
- текущая PK `(claim_id, evidence_id)` и одно поле `relation` заставляют выбирать только одно

Целевое решение:

- оставить один row на пару `(claim_id, evidence_id)`
- заменить `relation` на три независимых флага:
  - `supports_fact: bool`
  - `contradicts_fact: bool`
  - `illustrative: bool`

- добавить check-constraint:
  - `supports_fact` и `contradicts_fact` не могут быть `true` одновременно

Тогда один и тот же evidence может быть:

- support only
- counter only
- illustrative only
- support + illustrative
- counter + illustrative

Это важно для narrative preservation без потери factual grounding.

## Нормальная схема данных в итоге

- `Source` = что это за источник
- `EvidenceSpan` = какой именно canonical quote/fragment мы выделили
- `EvidenceProvenance` = где, кем и для какого research question этот evidence был найден
- `Claim` = атомарное утверждение
- `ClaimEvidence` = как именно evidence участвует в claim

## Целевой runtime flow

## 1. Source canonicalization и source dedupe

До подсчёта "независимых источников" система должна оперировать не сырым `source_id`, а canonical source identity:

- если `Source.is_duplicate_of` заполнен, canonical source = `is_duplicate_of`
- иначе canonical source = `Source.id`

Это правило должно использоваться везде, где считается независимость источников:

- verifier
- evidence dedupe
- claim dedupe heuristics, если там учитывается source diversity

Иначе два source rows с одним и тем же URL будут считаться двумя независимыми источниками.

## 2. Canonical evidence creation

Одинаковый quote не должен сохраняться как несколько независимых `EvidenceSpan`.

Exact dedupe evidence должен идти по ключу:

- `canonical_source_identity`
- `normalized_quote`
- `normalized_locator`

Где:

- `canonical_source_identity` = `coalesce(source.is_duplicate_of, source.id)`
- `normalized_quote` строится тем же нормализатором текста, который уже есть в Hunter
- `normalized_locator` нужен, чтобы не схлопнуть одинаковые короткие quotes из разных мест документа

Правило для `null locator` должно быть явным:

- если locator есть, он входит в dedupe key
- если locator отсутствует, dedupe не блокируется
- то есть при `null locator` exact dedupe идёт по:
  - `canonical_source_identity`
  - `normalized_quote`

Это осознанное решение.
Отсутствие locator не должно порождать клоны одного и того же quote.

Результат:

- в системе хранится один canonical `EvidenceSpan`
- все вопросы и все retrieval-contexts вешаются на него через `EvidenceProvenance`

## 3. Provenance writing

После создания или нахождения canonical `EvidenceSpan` всегда создаётся provenance-row.

### Hunter

`hunter.py` должен:

- искать существующий canonical evidence
- если не найден, создавать `EvidenceSpan`
- затем всегда писать `EvidenceProvenance`:
  - `question_id = q["id"]`
  - `branch_id = q["branch_id"]`
  - `retrieved_by = "hunter"`
  - `generation_run_id = state["generation_run_id"]`

### Librarian Fast

`librarian_fast.py` и `tools.py:create_web_evidence()` должны:

- работать через ту же canonical evidence creation path
- писать provenance-row с текущим gap:
  - `question_id`
  - `branch_id`
  - `retrieved_by = "librarian_fast"`
  - `generation_run_id`

### Deep Ingestion

Если после углублённой обработки появляется новый evidence, он тоже получает provenance-row:

- `retrieved_by = "deep_ingestion"`
- `question_id` и `branch_id` не могут быть `NULL`
- deep ingestion обязан наследовать provenance-контекст от source/task, который породил этот ingestion

То есть deep ingestion не создаёт "безродный" provenance-row.
Он продолжает уже существующий research context.

## 4. Claim builder input assembly

`claim_builder` не должен получать плоский список минималистичных evidence rows.

На входе для каждого evidence нужны:

- self data:
  - `id`
  - `verbatim_quote`
  - `asset_type`
  - `narrative_value_score`
  - `reliability_hint`
- source data:
  - `source_title`
  - `source_url`
  - `source_type`
  - `domain_reliability`
  - `authority_type`
  - `is_temporary`
  - `published_at`
- compact provenance summary:
  - `branch_ids`
  - `question_ids`
  - `retrieved_by`

Пример payload item:

```json
{
  "id": "evidence-id",
  "verbatim_quote": "quote",
  "asset_type": "mechanism",
  "narrative_value_score": 0.7,
  "source": {
    "title": "HKMA explainer",
    "url": "https://...",
    "type": "web",
    "domain_reliability": "high",
    "authority_type": "primary",
    "is_temporary": false,
    "published_at": "2024-01-10"
  },
  "lineage": {
    "branch_ids": ["b3", "b3.1"],
    "question_ids": ["b3.1"],
    "retrieved_by": ["hunter"]
  }
}
```

В prompt не нужно передавать полный retrieval history:

- не нужны все timestamps
- не нужны все query_text values
- не нужны полные поисковые trace’ы

Для LLM нужен только компактный контекст, а не весь audit trail.

## 5. Batching claims

`claim_builder` нельзя батчить случайной нарезкой массива evidence.

Целевая схема batching:

1. Сначала собрать provenance rows текущего run
2. Сгруппировать их по `branch_id`
3. Для каждой ветки взять union canonical evidence, связанных с вопросами этой ветки
4. Если ветка слишком большая, дробить её по token budget внутри этой же ветки
5. Генерировать claims локально на уровне branch-batch
6. После всех локальных батчей запускать global claim dedupe

Почему `branch`, а не `question`, как основной unit:

- question-level batching слишком мелкий
- он повышает шанс, что один и тот же смысл разъедется по соседним вопросам
- branch-level batching лучше удерживает общий смысл ветки
- stop/error локализуется в одной теме, а не в случайном хвосте массива

Важно:

- evidence не "принадлежит" ветке
- ветка используется только как runtime grouping для конкретного прогона claim builder

## 6. Global claim dedupe

Даже при canonical evidence и branch batching кросс-батчевые дубли claims всё равно возможны.

Поэтому после локальной генерации claims нужен отдельный global claim dedupe pass.

Минимальная логика:

- нормализация statement
- нормализация claim_type
- нормализация attribution
- collapse exact и near-exact duplicates
- merge evidence links

Near-exact dedupe должен быть deterministic и консервативным.

Правило merge:

1. Сначала exact merge:
   - одинаковый `claim_type`
   - одинаковый `normalized_statement`
   - одинаковый `normalized_attribution`
2. Затем near-exact merge только если одновременно выполняются условия:
   - одинаковый `claim_type`
   - совпадают извлечённые числа и даты
   - совпадает normalised attribution
   - высокий token-overlap между statements
   - claims опираются на пересекающиеся evidence/source/provenance neighborhoods

Near-exact dedupe не должен использовать LLM и не должен опираться на loose semantic similarity.
Иначе слишком высок риск схлопнуть два близких, но разных факта.

Это должен быть отдельный шаг до verifier.

Иначе:

- verifier увидит фальшивую "массу подтверждений"
- planner переоценит важность тем, которые просто чаще перефразированы

## Целевая политика claim building

## 1. FACT / REPORT / OPINION

Нужно оставить текущую тройку типов:

- `FACT`
- `REPORT`
- `OPINION`

Но жёстче описать границы:

- `FACT` = атомарное утверждение о мире, которое можно проверять по evidence
- `REPORT` = утверждение о том, что кто-то что-то сообщал, заявлял, писал или наблюдал
- `OPINION` = чья-то оценка, интерпретация или позиция

`authority_type` влияет на verifier для `FACT`, но не должен ломать корректные `REPORT`-claims вида:

- "Bloomberg сообщал ..."
- "Wikipedia пишет ..."

Для таких случаев вторичный источник может быть достаточным evidence именно для `REPORT`, потому что claim описывает сам акт сообщения.

## 2. Narrative preservation

Narrative preservation должно решаться двумя вещами одновременно:

- prompt rules в `claim_builder`
- relation-моделью `ClaimEvidence`, которая позволяет одному evidence быть и support, и illustrative

Правила для prompt:

- `CASE`, `ANECDOTE`, `EXPERT_QUOTE` по умолчанию идут в `illustrative`
- если такой evidence ещё и несёт отдельный атомарный факт, он может быть одновременно:
  - `supports_fact = true`
  - `illustrative = true`
- `MECHANISM`, `STATISTIC`, `TIMELINE`, `DEFINITION` по умолчанию идут в factual support, если они реально подтверждают claim

Цель:

- не терять hooks, scenes и contrast
- не превращать весь research inventory в сухой factual list

## Целевая политика verification

## 1. Что verifier обязан видеть

Verifier должен работать не только с quote и `domain_reliability`.

Он должен получать по каждому supporting evidence:

- `quote`
- `source_title`
- `source_url`
- `source_type`
- `published_at`
- `domain_reliability`
- `authority_type`
- `is_temporary`
- canonical source identity

Считать независимые источники нужно по canonical source identity, а не по сырым `source_id`.

## 2. Duplicate sources не должны раздувать independent source count

При загрузке evidence для verifier нужно:

- игнорировать duplicate source rows как независимые источники
- либо сразу резолвить `canonical_source_id = coalesce(is_duplicate_of, id)`
- и считать independence по этому полю

Иначе claim может стать `VERIFIED` из-за одного и того же URL, сохранённого дважды.

## 3. Temporary source ceiling

Правило из `CLAUDE.md` должно быть реально реализовано в verifier:

- если все supporting evidence идут только из temporary sources, claim не может быть выше `OBSERVED`

Даже если LLM считает quote "убедительным", это не повод поднимать claim до `VERIFIED`.

## 4. Authority as ceiling, not as proof

`authority_type` должен использоваться как ограничитель уверенности, а не как автоматический апгрейд.

Корректное правило:

- отсутствие primary corroboration ограничивает потолок уверенности для `FACT`
- наличие `primary` не должно само по себе автоматически поднимать claim в `VERIFIED`

Иначе document-level proxy начнёт выдавать ложные verified claims.

## 5. Rule-based policy для FACT

Базовая policy-логика для factual claims:

- `VERIFIED` возможно только если:
  - есть `2+` независимых canonical sources без противоречия, или
  - есть один strong authoritative source и LLM подтверждает прямое соответствие quote -> claim
- `OBSERVED`:
  - один источник
  - вторичные/третичные материалы
  - temporary-only support
  - weak but plausible semantic support
- `UNVERIFIED`:
  - evidence не подтверждает claim
  - claim слишком широкий относительно quotes
  - attribution/semantic mapping не сходится
- `DISPUTED`:
  - есть counter evidence или явное противоречие

## 6. Policy для REPORT / OPINION

Текущий shortcut "есть любой `attribution.who` -> OBSERVED" слишком слабый.

Нужно проверять качество attribution:

- `who` не должен быть пустой или бессодержательной заглушкой
- `where` и/или `when` должны использоваться, когда они есть в evidence
- "неустановленный эксперт", "источник", "наблюдатель" без нормальной идентификации не должны автоматически давать `OBSERVED`

Для `REPORT` и `OPINION` verifier должен проверять:

- есть ли реально quoted/attributed support
- соответствует ли формулировка claim тому, что источник действительно говорил

## 7. needs_research должен порождать новые gaps

Если verifier возвращает `needs_research=true` или если факт остаётся `UNVERIFIED`, он должен создавать новые `research_gaps` в том же формате, который понимает `research_fast_web`.

То есть loop должен работать так:

- verifier анализирует, чего именно не хватает
- verifier пишет свежие `research_gaps` по реальным provenance-парам `(branch_id, question_id)`, а не по synthetic Cartesian product
- graph возвращает управление в `research_fast_web`
- fast web ищет уже по обновлённым gaps, а не по историческим gaps из Hunter

Без этого verifier loop остаётся формальным и не улучшает доказательность claims.

## Целевая политика retrieval для Librarian

## 1. Не заменять broad query на targeted query

Для gap-based web research нельзя выбирать между:

- semantic broad query
- asset-targeted query

Нужны оба.

Правильная схема:

1. broad search по формулировке research question
2. targeted search по `missing_assets` и ключевым сущностям
3. union результатов
4. rerank / dedupe

Это даёт:

- semantic recall
- lexical precision
- меньше шансов, что разные gaps приведут к одному и тому же broad explainer

## 2. Language-aware query rewrite для keyword retrieval

Язык подкаста и язык retrieval не должны быть жёстко связаны.

Keyword retrieval должен работать в языке корпуса или в языках корпуса.

Если corpus language не совпадает с language generation, нужен явный query rewrite слой для retrieval:

- research question -> retrieval query in corpus language
- плюс целевые keywords из `missing_assets`

Примеры:

- RU question + EN corpus -> EN keyword query
- EN question + DE corpus -> DE keyword query
- mixed-language corpus -> несколько keyword variants по языкам корпуса

Иначе hybrid retrieval вырождается в vector-only или резко теряет lexical precision.

## Что именно менять в коде

## Модели

- `backend/app/models/source.py`
  - добавить `authority_type`

- `backend/app/models/evidence.py`
  - `EvidenceSpan` оставить canonical evidence без `question_id`
  - добавить новую модель `EvidenceProvenance`

- `backend/app/models/claim.py`
  - заменить `ClaimEvidence.relation` на:
    - `supports_fact`
    - `illustrative`

## Research pipeline

- `backend/app/pipeline/phase3_research/hunter.py`
  - canonical evidence lookup/create
  - provenance insert на каждый retrieval hit
  - exact evidence dedupe до сохранения

- `backend/app/pipeline/phase3_research/tools.py`
  - `create_web_evidence()` должна работать через canonical evidence path

- `backend/app/pipeline/phase3_research/librarian_fast.py`
  - broad + targeted query union
  - provenance insert
  - работа по свежим `research_gaps`

## Claims pipeline

- `backend/app/pipeline/phase4_claims/claim_builder.py`
  - load by branch batches, not flat 24-row chunks
  - join `EvidenceSpan -> Source`
  - aggregate `EvidenceProvenance` в compact summary
  - усилить prompt rules для narrative preservation
  - писать оба relation-флага в `ClaimEvidence`

- отдельный шаг после `claim_builder`
  - global claim dedupe / merge

- `backend/app/pipeline/phase4_claims/verifier.py`
  - считать independence по canonical source identity
  - учитывать `is_temporary`
  - учитывать `authority_type` как ceiling-signal
  - валидировать attribution quality для `REPORT/OPINION`
  - писать свежие `research_gaps`

- `backend/app/pipeline/graph.py`
  - loop `verify -> research_fast_web` должен идти по свежим verifier-generated gaps

## Рекомендуемый порядок внедрения

Порядок ниже не является "переходной архитектурой". Это просто порядок работ для внедрения целевой модели.

### Шаг 1. Данные и связи

- `authority_type` на `Source`
- `EvidenceProvenance`
- новая модель `ClaimEvidence`
- canonical source identity и exact evidence dedupe

### Шаг 2. Claim building

- branch-based batching
- compact provenance summary в prompt
- narrative preservation rules
- global claim dedupe

### Шаг 3. Verification и research loop

- duplicate-safe independent source counting
- temporary-source ceiling
- attribution quality checks
- verifier-generated `research_gaps`

### Шаг 4. Retrieval quality

- broad + targeted librarian search
- language-aware keyword rewrite

## Итог

Качественное решение здесь не в том, чтобы "добавить ещё одно поле".

Нужна целостная перестройка контракта между слоями:

- evidence хранится канонически
- lineage хранится отдельно и явно
- claims строятся по branch provenance groups
- duplicates схлопываются до verifier
- verifier считает независимость по canonical sources и реально знает про temporary/authority signals
- research loops получают новые gaps, а не гоняют старые

Только в такой схеме downstream planning и writing будут работать не с шумным плоским инвентарём, а с управляемой исследовательской картой.
