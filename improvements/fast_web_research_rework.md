# Fast Web Research Rework

## Зачем нужен этот документ

Текущий `fast web research` уже лучше, чем раньше:

- есть English rewrite для неанглоязычных вопросов
- убран старый `[:3]` cap по gaps
- governor теперь считает обработанные gaps
- есть dedupe на уровне `Source` и `EvidenceSpan`

Но при этом сама фаза всё ещё устроена слишком грубо:

- search/fetch/extract path слишком шумный
- prompt extractor'а получает грязный prefix страницы
- уже известные URLs могут заново тратить search/fetch/LLM budget
- verifier-loop gaps слишком бедные и плохо направляют `fast web`

Это особенно болезненно потому, что `fast web` запускается именно в тех местах, где пайплайн уже начинает сжигать реальные деньги:

- expensive loop iterations
- verifier follow-up
- deep-ingestion tail

Ниже зафиксировано, что именно нужно менять.

## Короткий диагноз

Сейчас `fast web` работает так:

1. берёт `research_gaps`
2. строит query variants
3. ищет через Tavily
4. fetch'ит top pages через Jina
5. кладёт в extractor prompt первые `5000` символов страницы
6. LLM пытается вытащить 1-3 evidence items
7. только потом происходит source/evidence dedupe

Главные проблемы этой схемы:

- дешевая локальная очистка почти не используется
- дорогое LLM extraction тратит контекст на boilerplate
- обрезка страницы идёт по позиции, а не по релевантности
- verifier-loop даёт weak gap payload
- уже известные project sources не отсекаются до fetch/extract

## Принципы целевой реализации

1. `fast web` должен оставаться быстрым, но не быть "грязным shortcut'ом".
2. Дешёвую локальную очистку нужно делать до дорогого LLM extraction.
3. Если canonical source уже известен проекту и fully ingested с доступными chunks, нельзя снова тратить на него network + LLM budget без причины.
4. В prompt нужно класть не "первые N символов", а релевантные excerpt windows.
5. `Hunter gaps` и `Verifier gaps` должны одинаково хорошо направлять поиск, но разными типами сигналов.

## P0. Что нужно изменить обязательно

### 1. Убрать Jina-only fast path как основной extractor

Сейчас `fast web` fetch'ит страницу через Jina и почти без очистки отдаёт её в extractor.

Это хуже normal ingestion path, где уже есть:

- `mediawiki` path
- `trafilatura`
- Jina fallback
- web cleanup / quality gates

Нужно:

- вынести лёгкий shared extractor helper для `fast web`
- этот helper должен использовать ту же extractor hierarchy, что и normal ingestion:
  - `mediawiki` when applicable
  - `trafilatura`
  - `jina` as fallback
- после extraction всегда прогонять `clean_web_text(...)`

Важно:

- `fast web` не обязан делать полный ingestion
- но cheap extraction + cleanup должен быть тем же качественным слоем, что и в ingestion

Иначе pipeline экономит на дешёвой очистке и тратит дорогой LLM контекст на мусор.

### 2. Убрать blind prefix truncation страницы

Сейчас extractor получает `content[:5000]`.

Это плохой дизайн, потому что:

- нужный evidence может быть не в начале страницы
- у noisy sites начало часто занято login/signup/nav/share мусором
- page selection по позиции не имеет отношения к question relevance

Нужно:

- перестать класть в prompt "первые 5000 символов"
- вместо этого строить `relevance excerpt windows` из очищенного текста

Предпочтительная схема:

- после extraction + cleanup разбить текст на абзацы/блоки
- посчитать cheap relevance score каждого блока по:
  - overlap с `question`
  - overlap с named entities / dates / currencies / acronyms
  - overlap с `missing_assets` keywords
  - overlap с claim text / gap signal, если это verifier-loop
- выбрать top windows

Целевой prompt payload на страницу:

- `title`
- `url`
- `query_text`
- `excerpt_windows`: 2-4 куска по 800-1600 chars
- `content_strategy`: например `relevance_windows`

То есть в prompt должен идти не `raw prefix`, а curated relevant slices.

### 3. Делать URL reuse до fetch/extract, а не после

Сейчас canonical source dedupe есть только в `create_web_evidence(...)`.

Это защищает БД, но не защищает деньги.

Нужно:

- до `fetch_page_tool(...)` canonicalize каждый candidate URL
- проверять, есть ли он уже среди project sources

Дальше правила должны быть такие:

#### Если canonical source уже есть, fully ingested и его content уже доступен в local chunks

- не fetch'ить URL заново
- не звать page extractor
- не считать такой URL полезным fast-web candidate
- переходить к следующему novel URL из search results

Важно:

- `fast web` по смыслу должен приносить **новую внешнюю информацию**
- already-ingested URL не даёт `open-web delta`
- повторное использование такого URL внутри fast-web только тратит search/fetch/extraction budget без реального прироста

Если позже потребуется отдельный механизм для "поднять уже существующий локальный материал в нужную ветку", это должен быть
другой path, не `fast web`.

#### Если source уже есть, но ещё не fully ingested

- не создавать повторный temp/fetch loop без причины
- если текст уже есть в preview/metadata и его достаточно, использовать его
- если нет, просто приоритизировать deep ingestion / дождаться existing ingestion path

Минимум:

- нельзя снова network-fetch'ить и LLM-extract'ить already-known URL вслепую

### 4. Сделать verifier gaps полезными для search

Сейчас `Hunter gaps` и `Verifier gaps` живут по разным правилам:

- Hunter gap:
  - содержит `missing_assets`, `required_assets`, `expected_assets`
- Verifier gap:
  - эти поля пустые
  - есть только branch/question/intent/found_assets/claim_id

Это делает verifier-loop fast-web слишком тупым.

Нужно:

- не оставлять verifier gaps пустыми по search guidance
- дать им отдельную, но полезную структуру

Минимальный целевой набор для verifier gap:

- `gap_origin = "verifier"`
- `claim_id`
- `claim_statement`
- `question_id`
- `branch_id`
- `intent`
- `found_assets`
- `support_strength`
  - например `single_source`
  - `contextual_only`
  - `weak_authority`
  - `needs_independent_confirmation`
- `search_objective`
  - короткое описание того, что надо дожать

Это не обязательно должны быть те же самые `missing_assets`, что у Hunter.
Но search phase должен понимать не просто "что-то не так", а "что именно надо искать сильнее".

### 5. Fast web должен искать corroboration, а не просто ещё один текст по теме

Для verifier-loop основной remaining problem сейчас обычно не "вообще нет материала", а:

- single-source support
- contextual-only support
- weak secondary corroboration

Значит fast-web должен уметь строить queries не только от вопроса, но и от слабого claim.

Нужно:

- в verifier-loop строить query variants ещё и от `claim_statement`
- выделять:
  - ключевые entities
  - даты
  - суммы/проценты
  - механизмы
- искать `independent confirmation`, а не generic explainer

Пример принципа:

- не просто `why did Hong Kong delegate note issuance`
- а `Hong Kong note issuance delegated to private banks legal basis official`

Важно:

- не каждый verifier gap закрывается corroboration-query tuning
- есть structural / interpretive gaps, для которых в open web может не быть прямого подтверждения в нужной форме
- для таких gaps pipeline должен уметь остановиться на честном `OBSERVED/contextual`, а не бесконечно тратить loop budget на безнадёжный поиск

## P1. Что нужно сильно улучшить после P0

### 6. Разделить fast-web режимы: initial gap fill и verifier follow-up

Сейчас это одна и та же фаза с почти одинаковой логикой.

Но по смыслу это разные задачи:

#### Initial fast-web after Hunter

Ищет:

- missing asset types
- baseline external evidence
- story material, если в corpus пусто

#### Verifier-loop fast-web

Ищет:

- corroboration
- better source family
- direct confirmation instead of contextual support

Нужно сделать две разные query policies внутри одной фазы:

- `mode = hunter_gap_fill`
- `mode = verifier_corroboration`

И отдельно разделить бюджеты:

- `fast_web_initial_gap_budget`
- `fast_web_verifier_gap_budget`

Почему это важно:

- initial fast-web и verifier-loop решают разные задачи
- их нельзя считать одним накопительным governor counter
- иначе initial pass может "съесть" budget, который потом нужен verifier-loop для дорогих remedial queries

### 7. Улучшить page reranking перед fetch

Сейчас search results объединяются по URL, сортируются по Tavily score, и дальше берутся top-2 pages.

Нужно добавить cheap reranking до fetch:

- novelty relative to already-known canonical sources
- domain preference:
  - official / archive / institution
  - research / primary docs
  - only потом explainers
- overlap с search objective

Тогда top pages будут:

- не просто "высокий Tavily score"
- а "лучшие кандидаты именно под этот gap"

### 8. Прокинуть больше контекста в extractor prompt

Сейчас extractor видит:

- question
- intent
- optional assets
- web pages

Для verifier-loop этого мало.

Нужно добавить:

- `gap_origin`
- `claim_statement` when applicable
- `search_objective`
- `existing_support_summary`
  - например `single medium tertiary source`
  - `contextual-only support`

Тогда extractor сможет выбирать evidence не просто "по теме", а именно под задачу loop repair.

## P1. Query generation improvements

### 9. Query generation должен опираться не только на question text

Сейчас native targeted query строится из:

- `question`
- named tokens
- asset keywords

Это лучше, чем раньше, но для verifier-loop этого мало.

Нужно:

- строить queries от:
  - question
  - claim statement
  - structured search objective

### 10. English rewrite должен быть обязательным query variant для non-English web search

Сейчас это уже почти так, но это нужно зафиксировать как invariant:

- если вопрос не английский, хотя бы один English query variant обязателен
- native query можно сохранять как дополнительный variant

То есть принцип:

- `non-English question -> English web retrieval is mandatory, not optional`

### 11. Named token extraction не должен ломаться на многоязычных вопросах

Сейчас regex уже unicode-aware, это хорошо.

Но нужно сохранить этот инвариант:

- даты, суммы, валюты, acronyms, named entities должны переживать query rewrite и targeted query build

## P1. Reuse existing project knowledge

### 12. Fast web должен жёстко пропускать already-ingested URLs

Если URL уже известен проекту, а source fully ingested и его content доступен в local chunks:

- его нужно исключать из fast-web candidate set
- не тратить на него fetch/extract budget
- продолжать downrank/scan по search results, пока не найдётся новый URL

Это особенно важно для expensive loops:

- иначе pipeline платит за Tavily + fetch + extraction там, где ничего нового уже не появится

Если позже понадобится отдельная логика `local evidence resurfacing`, её нужно проектировать отдельно, а не смешивать с
`fast web`.

## P1. Observability

### 13. Debug dump fast-web должен показывать не только результат, но и waste

Сейчас при анализе fast-web приходится догадываться, где именно были потери.

Нужно явно логировать:

- selected gaps
- built query variants
- search results per query
- URLs skipped because already known
- pages dropped by quality filter
- excerpt windows sent to extractor
- extractor output accepted vs rejected
- why evidence item was filtered out

Иначе дорогие прогоны снова будут разбираться постфактум по косвенным признакам.

## P2. Что можно делать после основных правок

### 14. Не тратить fast-web budget на low-value pages

Это полезный hardening, но не P0.

После extraction + cleanup нужна cheap quality filter:

- минимальный объём полезного текста
- отсутствие явного boilerplate domination
- отсутствие blocking / shell content
- достаточный overlap с вопросом/claim

Если страница не проходит filter, её не надо отправлять в extractor LLM вообще.

### 15. Adaptive fetch depth

Сейчас top-2 pages на gap — это жёсткая константа.

Можно сделать:

- low-risk gaps -> 2 pages
- verifier corroboration gaps -> 3-4 pages when needed

Но это уже optimisation, а не базовая correctness fix.

### 16. Cross-gap source family diversification

Можно добавить soft policy:

- если gap уже держится на Википедии + explainers,
- новые query variants должны сильнее толкать pipeline к другой source family

Но это уже вторичный слой после того, как появится нормальный verifier-loop guidance.

## Предлагаемый порядок реализации

### P0

1. shared extraction/cleanup path for fast-web
2. relevance windows instead of `content[:5000]`
3. pre-fetch skip/handling of known canonical sources
4. useful verifier gap payload
5. corroboration-oriented fast-web mode

### P1

6. split policies for Hunter-gap vs Verifier-gap
7. reranking before fetch
8. split budgets for initial fast-web vs verifier-loop fast-web
9. richer extractor payload
10. stronger query generation from claim + search objective
11. better fast-web debug dumps

### P2

12. cheap page quality filter before extractor LLM

## Самая короткая суть

Сейчас `fast web` слаб не потому, что у него нет search или LLM.

Он слаб потому, что:

- слишком поздно делает dedupe/skip known URLs
- слишком рано режет страницу
- кладёт в prompt грязный prefix вместо релевантных фрагментов
- verifier-loop слишком плохо объясняет, что именно надо искать
- initial и verifier-loop budgets пока не разведены по задачам

То есть следующий этап качества здесь не "ещё один prompt tweak", а
приведение `fast web` к нормальной retrieval discipline:

- clean input
- relevant excerpts
- useful gap payload
- no duplicate waste
- corroboration-oriented search
