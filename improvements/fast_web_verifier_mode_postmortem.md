# Fast-Web Verifier Mode Postmortem

## Контекст

После последних фиксов fast-web перестал терять partial search results, научился резать monster queries и стал дешевле в loop'е.  
Но новый прогон показал, что в `verifier`-режиме fast-web всё ещё даёт плохие extractor prompts и нерелевантные страницы.

Статус:

- core P0/P1 redesign из этого постмортема реализован в Round 71
- документ сохранён как разбор корневой причины и объяснение, почему понадобилась эта переделка

Это не тот же класс проблем, что раньше.

- Раньше fast-web страдал от runtime / cost / orchestration багов.
- Сейчас главная проблема — **неправильная смысловая постановка задачи в verifier-loop**.

## Симптом

В verifier-origin prompt одновременно оказались:

- `research_question`: вопрос про **currency board** и устойчивость частной эмиссии
- `claim_statement`: конкретный исторический claim про **США до 1865 года и 1600 банков**
- `required_evidence`: `DEFINITION + MECHANISM`
- `target_missing_assets`: `DEFINITION + MECHANISM`

На выходе fast-web выбрал страницы:

- OCC PDF про dual banking system
- FEDS paper 2025 про financial stability
- `uscurrency.gov` про защитные признаки банкнот

Это плохой prompt не потому, что конкретная страница случайно плохая, а потому что сам pipeline в verifier-loop даёт противоречивую задачу.

## Что реально происходит сейчас

### 1. Verifier-gap остаётся question-scoped

В [verifier.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py) `_build_research_gaps()` агрегирует gaps по `(branch_id, question_id)` и подтягивает из research tree:

- `required_assets`
- `expected_assets`
- `missing_assets`

Это происходит здесь:

- [verifier.py#L538](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L538)
- [verifier.py#L545](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L545)
- [verifier.py#L546](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L546)

Одновременно туда же добавляется claim-level context:

- `claim_statement`
- `search_objective`
- `support_strength`
- `existing_support_summary`
- `claim_contexts`

Это значит, что один verifier-gap сейчас по модели данных уже является гибридом:

- `question gap fill`
- `claim corroboration`

### 2. Fast-web не разделяет эти два режима в extractor payload

В [librarian_fast.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py) verifier-gap идёт в `prompt_payload` так:

- `research_question`
- `claim_statement`
- `search_objective`
- `claim_contexts`
- `required_evidence`
- `expected_evidence`
- `target_missing_assets`

См.:

- [librarian_fast.py#L231](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L231)
- [librarian_fast.py#L239](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L239)

Именно здесь появляется логическая ошибка:

- verifier-loop должен искать **подтверждение слабого claim**
- но extractor prompt до сих пор заставляет модель ещё и удовлетворять question-level asset accounting

В результате prompt говорит одновременно:

- "подтверди claim про США до 1865"
- "найди definition/mechanism для currency board"

Это две разные цели.

## Главные проблемы по слоям

### Проблема A. Verifier-loop ищет и corroboration, и question assets одновременно

Это корневая ошибка.

Для verifier-origin gap primary objective должен быть:

- `corroborate / sharpen / directly support weak claim`

Сейчас primary objective размазан между:

- claim-level corroboration
- question-level asset-filling

Следствие:

- query builder производит смешанные запросы
- reranker считает релевантными страницы, которые отвечают только на вопрос в широком смысле
- extractor пытается извлечь `DEFINITION` и `MECHANISM`, даже если gap возник из-за слабого исторического claim

### Проблема B. Query generation смешивает question text и claim text

В `verifier_corroboration` mode `_build_native_search_queries()` добавляет сразу несколько классов запросов:

- сам `question`
- `question + targeted terms`
- `claim_statement`
- `claim_statement + targeted terms`
- `question + search_objective`

См.:

- [librarian_fast.py#L634](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L634)
- [librarian_fast.py#L653](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L653)
- [librarian_fast.py#L670](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L670)

Из-за этого verifier-loop тащит в SERP одновременно:

- broad question queries
- direct claim queries
- official corroboration queries

Это даёт шумный результат.

Для verifier-loop broad `question` query вообще не должен быть обязательным.  
Он полезен для Hunter / initial fast-web, но не для claim corroboration.

Дополнительная проблема: current rewrite layer вообще не заменяет native queries, а только добавляет English variants сверху.

То есть даже когда LLM rewrite срабатывает, pipeline всё равно продолжает искать по:

- длинным исходным вопросам
- длинным claim statements
- их deterministic склейкам

Это плохой дизайн не только для русского языка.  
Даже если подкаст и research tree уже на английском, такие raw query strings всё равно остаются плохими retrieval queries.

Следствие:

- language rewrite не решает query quality
- verifier-mode продолжает тащить noisy natural-language strings в web search
- проблема будет воспроизводиться и на англоязычных темах

### Проблема C. `missing_assets` pollute verifier queries

`_build_targeted_query()` добавляет keywords от `missing_assets`:

- `DEFINITION -> definition meaning what is`
- `MECHANISM -> mechanism how it works process`

См.:

- [librarian_fast.py#L685](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L685)

Это имеет смысл для initial gap fill.  
Но в verifier-loop это часто вредит.

В разобранном prompt получилось именно это:

- claim про historical US private bank issuance
- query polluted словами `definition / mechanism`

То есть query ушёл не за подтверждением claim, а в сторону broad explanatory material.

### Проблема D. Reranking считает релевантность по объединённому мешку терминов

`_rerank_search_results()` строит `objective_terms` из:

- `question`
- `claim_statement`
- `search_objective`
- `missing_assets`

См.:

- [librarian_fast.py#L947](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L947)

То же самое делает `_is_low_value_page()` и `_select_excerpt_windows()`:

- [librarian_fast.py#L1144](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L1144)
- [librarian_fast.py#L1159](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L1159)

Это приводит к системной ошибке:

- если страница "про банки / стабильность / эмиссию / систему", она получает overlap bonus
- даже если она не подтверждает нужный claim

Поэтому в prompt попадают:

- general financial stability papers
- loosely related institutional pages
- currency pages, где нет нужного факта

### Проблема E. Page/window scoring почти не требует claim-anchor match

Сейчас excerpt scoring:

- считает token overlap
- даёт `named_bonus` за наличие uppercase/digit tokens

См.:

- [librarian_fast.py#L1212](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L1212)

Но он не требует жёсткого совпадения claim anchors:

- дат
- чисел
- specific entities
- ключевых юридических/исторических терминов из claim

Из-за этого bibliography, appendix, index-like fragments и просто соседние разделы могут пройти как "релевантные windows".

### Проблема F. Extractor prompt не знает, что для verifier-gap question assets вторичны

Системный prompt extractor'а сейчас универсальный и говорит:

- если `target_missing_assets` не пустой, приоритизируй их
- для verifier gaps приоритизируй corroboration

См.:

- [librarian_fast.py#L33](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L33)

Это противоречивое правило.  
Когда оба сигнала присутствуют, LLM не знает, что важнее.

## Почему прошлые фиксы не решили это

Прошлые фиксы были в основном про:

- runtime stability
- cost control
- duplicate avoidance
- incremental verification
- richer verifier context

Они решали правильные проблемы, но другого класса.

Они не сделали главного:

- **не отделили `initial question gap fill` от `verifier claim corroboration` как две разные retrieval задачи**

Сейчас это всё ещё один и тот же fast-web machinery с разными флажками.  
Этого недостаточно.

## Что надо поменять

## P0

### 1. Для `gap_origin = verifier` перестать прокидывать question-level asset targets как primary objective

В verifier-loop extractor payload должен быть перестроен так:

- primary:
  - `claim_contexts`
  - `search_objective`
  - `support_strength`
  - `existing_support_summary`
- secondary background:
  - `research_question`
  - `question_intent`

Для verifier-origin gaps:

- `required_evidence`
- `expected_evidence`
- `target_missing_assets`

не должны использоваться как extraction target.

Лучший вариант:

- либо вообще не передавать их в extractor prompt для verifier gaps
- либо передавать отдельно как `question_background_assets`, явно помечая, что это не primary objective

### 2. Ввести отдельный verifier extractor mode

Нужны два разных extractor contracts:

1. `question_gap_fill`
2. `claim_corroboration`

Во втором режиме extractor должен отвечать на вопрос:

- "этот quote прямо подтверждает один из слабых claims?"

А не:

- "какой интересный evidence подходит к теме вопроса?"

### 3. Для verifier-loop перестроить query generation вокруг claim anchors, а не question text

В verifier mode query priority должна быть такой:

1. claim-specific direct query
2. official / archival / legal corroboration query
3. claim-specific entity/date/number query

А не:

1. broad question
2. question + missing asset keywords
3. claim + question mixture

`question` в verifier mode должен быть максимум fallback, а не базовый query.

### 4. Сделать universal LLM query synthesis и перестать использовать raw/native query strings как финальные search queries

Правильный принцип:

- web search queries должны всегда строиться через отдельный LLM synthesis step
- это правило должно работать для всех языков, включая английский
- language rewrite — это частный случай query synthesis, а не отдельная ветка поверх плохих native queries

То есть pipeline должен делать:

1. взять question / claim context
2. построить retrieval intent
3. попросить LLM вернуть короткие search-oriented query variants
4. отправлять в web search только эти финальные queries

А не делать:

1. построить сырые deterministic queries
2. иногда добавить поверх них English rewrite
3. искать и по хорошим, и по плохим query одновременно

### 5. Убрать deterministic `search_objective + asset keywords` query expansion

Для verifier-origin gaps `missing_assets` не должны добавлять:

- `definition`
- `mechanism`
- `case`
- и т.д.

в search queries и extractor targeting.

Иначе verifier corroboration снова превращается в broad topic search.

Более жёстко:

- `search_objective`
- `missing_assets`
- `asset keywords`

не должны механически конкатенироваться в query strings вообще.

Они могут использоваться только как **входной контекст для LLM query synthesis**, но не как deterministic string expansion.

### 6. Сделать claim-anchor gating перед extractor

Перед тем как страница или excerpt window попадают в extractor prompt, verifier-mode должен проверять:

- есть ли overlap по claim-specific anchors
- например:
  - даты
  - числа
  - организации
  - currency / legal terms

Если страница совпадает только с broad question terms, но не с claim anchors, её нужно дропать.

## P1

### 7. Развести scoring на question-mode и verifier-mode

Сейчас:

- `_rerank_search_results`
- `_is_low_value_page`
- `_select_excerpt_windows`

везде используют общий blended objective.

Нужно сделать:

- `question_gap_fill scoring`
- `claim_corroboration scoring`

Во втором режиме question terms должны иметь меньший вес, чем claim anchors.

### 8. Добавить специальные penalties для bibliography / appendix / table-like windows

Сейчас длинные PDF легко протаскивают:

- reference sections
- tables
- index-like fragments

Нужны дешёвые penalties для окон, где:

- слишком много citation-like fragments
- слишком много author-year patterns
- слишком мало prose sentences
- слишком много tabular separators

### 9. Сузить verifier search objective

Сейчас `_search_objective_for_gap()` формулирует длинные общие фразы вида:

- `Find a direct source that explicitly confirms: ...`

Это хороший человеческий intent, но плохой machine retrieval objective.

Нужна отдельная компактная retrieval form:

- short corroboration objective
- explicit anchor terms
- without whole natural-language claim pasted verbatim

## P2

### 10. Для structural / interpretive gaps уметь не запускать corroboration-loop как будто это обычный factual gap

Есть gaps, которые не чинятся простым corroboration search:

- философские / interpretive вопросы
- editorial synthesis questions

Для них verifier-loop должен уметь сказать:

- `web corroboration unlikely to help`

И не тратить budget так же, как на factual weak-support claims.

## Короткий вывод

Текущий verifier-loop fast-web уже не сломан как runtime-система.  
Но он всё ещё сломан как retrieval design.

Главная проблема:

- pipeline не разделяет
  - `question asset fill`
  - `claim corroboration`

Пока это не разведено, fast-web будет продолжать строить prompts, где:

- один вопрос про механизм
- один claim про исторический факт
- asset targets от вопроса
- страницы про что-то "рядом"

И на выходе будет получаться шумный, дорогой и плохо управляемый extractor.
