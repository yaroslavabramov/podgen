# Fast-Web Postmortem (2026-03-11)

## Контекст

Разбор остановленного verifier-loop `fast_web` для `run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352`.

Основной симптом:

- fast-web fetch'ит и кладёт в extractor prompt много страниц
- по первым gap'ам полезного delta почти нет
- в одном из prompt'ов `search_objective` почти дублирует `claim_statement`
- summary может показывать `new_evidence_count > 0`, но это число не эквивалентно "реально новый полезный evidence delta"

Ключевые логи:

- [b1 gap dump](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T001203832886Z_run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352_gap-b1-b1_3570eb7b2d.json)
- [b3 gap dump](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T001624517750Z_run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352_gap-b3-b3_f7c9e9d65a.json)
- [b4 gap dump](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T001921988905Z_run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352_gap-b4-b4_b3a5358413.json)
- [final summary](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T002147336534Z_run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352_summary_d1c6bb2161.json)

## Что произошло по факту

### 1. `b1`: pages fetched, extractor дал `0` items

По `b1` fast-web:

- сгенерировал 4 queries
- отранжировал 16 search results
- выбрал и зафетчил 3 page candidates
- в prompt попали:
  - law review PDF про racism / colonial administration
  - treaty PDF с таблицами и оглавлением
  - `British Hong Kong` Wikipedia

Но extractor вернул пустой список.

Это не surprise: удержанные pages были слабо релевантны именно claim-корроборации про ранние деньги в Гонконге.

### 2. `b3`: extractor нашёл 3 хороших items, но pipeline выкинул их все

Это самый важный баг этого run.

В [b3 gap dump](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T001624517750Z_run-28905a5b-f1f2-49d6-bfc8-dcceb1b28352_gap-b3-b3_f7c9e9d65a.json):

- extractor_mode = `claim_corroboration`
- fetched pages:
  - `bep.gov`
  - `federalreservehistory.org`
  - `gilderlehrman.org`
- LLM parsed 3 items, в том числе:
  - `By 1860 there were 1,500–1,600 such banks...`
  - `Congress in 1865 passed a prohibitive tax on state bank notes...`

Но все 3 item были rejected с причиной:

- `asset_type_not_targeted`

Почему:

- в `research_fast_web()` post-filter всё ещё использует `target_missing_assets` независимо от extractor mode:
  - [librarian_fast.py:164](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L164)
  - [librarian_fast.py:281](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L281)
- для verifier-gap `target_missing_assets` пришёл из question-level `missing_assets`
- extractor честно вернул `STATISTIC` / `TIMELINE`
- pipeline выкинул их, потому что ждал другой asset type

Это прямое противоречие текущему design intent:

- verifier-mode должен работать как `claim corroboration`
- но фактически он всё ещё подчинён question-level asset accounting на этапе accept/reject

## Главные проблемы

### P0. Verifier-mode post-filter всё ещё подчиняется `target_missing_assets`

Это главный реальный баг.

Сейчас verifier-mode prompt уже переведён в `claim_corroboration`, но accept/reject логика осталась старой.

Следствие:

- fast-web может найти прямую corroboration для claim
- extractor может её корректно вернуть
- pipeline потом сам выбрасывает её как "не тот asset type"

Это и даёт картину "мы фетчим, но новых evidence нет".

### P0. `search_objective` дублирует `claim_statement`

В verifier gap:

- `claim_statement` уже содержит, что нужно подтвердить
- `search_objective` сейчас строится шаблоном `Find ... source that confirms: {claim}`
  - [verifier.py:436](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L436)
- при агрегации нескольких claim'ов он просто склеивается через ` | `
  - [verifier.py:512](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/verifier.py#L512)

Потом fast-web кладёт в payload и top-level `claim_statement`, и top-level `search_objective`, и ещё полный `claim_contexts`:

- [librarian_fast.py:223](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L223)

Результат:

- prompt раздут
- полезной новой информации мало
- extractor получает повтор одного и того же сигнала в трёх формах

### P1. Reranking / page selection всё ещё удерживает pages "рядом по теме", но не по claim

По `b1` реально полезные monetary/history pages были либо понижены, либо dropped, а в prompt прошли:

- treaty tables
- legal review с широким colonial context
- Wikipedia article с общим историческим фоном

Это значит, что текущий cheap pre-LLM selection всё ещё недостаточно хорошо работает именно для verifier corroboration.

Проблема здесь не обязательно в "мало эвристик". Проблема в том, что even after verifier-mode redesign, page selection всё ещё не гарантирует:

- "эта страница содержит прямое подтверждение claim"

## Отдельная проблема наблюдаемости

### `StepExecutionStopped` проглатывается внутри fast-web search loop

Ты остановил шаг вручную, но для этого run всё равно появился `summary` dump.

Причина:

- `_run_search_queries()` ловит общий `except Exception`
  - [librarian_fast.py:837](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L837)
- `StepExecutionStopped` попадает туда же и превращается в обычный `search_failures[]`
- после этого fast-web продолжает идти дальше и пишет `summary`

Это не просто cosmetic issue. Это ломает step-mode semantics:

- user запросил stop
- шаг не должен продолжать как "обычная частичная деградация"

### `new_evidence_count` не равно "реально новый evidence delta"

В `research_fast_web()`:

- `new_evidence_count += 1` на каждый accepted item
  - [librarian_fast.py:305](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/librarian_fast.py#L305)

Но `create_web_evidence()` вызывает `get_or_create_canonical_evidence()` и игнорирует `created: bool`:

- [tools.py:334](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase3_research/tools.py#L334)
- [evidence_store.py:131](/Users/yaroslavabramov/side/podgen/backend/app/services/evidence_store.py#L131)

Значит summary может говорить:

- `new_evidence_count = 3`

хотя по факту это:

- 3 accepted extractor items
- не обязательно 3 новых canonical evidence rows

Это делает fast-web summary слишком оптимистичным и может расходиться с тем, что видно в UI/БД.

## Что именно надо поменять

### P0

1. В `claim_corroboration` режиме убрать post-filter `asset_type_not_targeted`.

Правило:

- question-level `target_missing_assets` можно использовать только в `question_gap_fill`
- verifier-mode не должен выкидывать extractor items из-за question asset mismatch

2. Упростить verifier-mode payload:

- убрать top-level длинный `search_objective` из extractor prompt
- заменить его компактным signal'ом, например:
  - `corroboration_goal = independent_second_source`
  - `corroboration_goal = direct_source_needed`
  - `corroboration_goal = stronger_authority_needed`

3. Оставить claim semantics в одном месте:

- `claim_contexts` как основной источник truth
- top-level `claim_statement` только как краткий aggregate summary, если вообще нужен

4. Не проглатывать `StepExecutionStopped` внутри fast-web.

Правило:

- step stop должен abort'ить весь шаг
- а не превращаться в очередной query failure

### P1

4. Пересмотреть verifier-mode page selection:

- меньше broad history/legal context pages
- больше приоритета pages, где claim anchors совпадают в body, а не только в query overlap

5. Исправить success metrics:

- различать:
  - `accepted_extractor_items`
  - `new_canonical_evidence_count`
  - `new_provenance_links_count`

Сейчас это смешано.

## Вывод

Проблема текущего run не в том, что "web ничего не нашёл".

По `b3` он как раз нашёл useful corroboration, но pipeline сам её выбросил.

То есть основной remaining defect сейчас такой:

- verifier-mode fast-web ещё не до конца развязан с question-level asset accounting

Это приводит к ситуации:

- search/fetch/extract тратят деньги
- полезный claim-level result появляется
- но accept/reject логика его не пропускает дальше

Это более серьёзно, чем просто "неудачный prompt".
