# Fast Web Filter And Budget Findings (2026-03-11)

## Контекст

Эти выводы основаны на прогоне `run-11a4826f-9688-4731-84cd-01bcadf92447`, особенно на verifier-loop fast-web.

Ключевые файлы:

- [fast web summary 1](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T093336903170Z_run-11a4826f-9688-4731-84cd-01bcadf92447_summary_f0807980cb.json)
- [fast web summary 2](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T101346049942Z_run-11a4826f-9688-4731-84cd-01bcadf92447_summary_ec6a7f47c9.json)
- [gap b1.2](/Users/yaroslavabramov/side/podgen/tmp/debug/fast_web/20260311T101137089310Z_run-11a4826f-9688-4731-84cd-01bcadf92447_gap-b1-b1-2_e5deb1b2f8.json)

## Главные выводы

### 1. Low-value filter работает заметно, но пока не заслуживает доверия

По `b1.2` filter выкинул не только слабые страницы, но и кандидатов, которые выглядели сильнее, чем реально fetched pages.

Примеры:

- Был отброшен `oelawhk.lib.hku.hk/items/show/1024`, хотя в snippet уже был прямой сигнал про `silver dollar` и `legal tender in Hongkong`.
- Был отброшен `barnard.edu` PDF, хотя это академический источник с релевантным snippet по серебру и монетарному контексту.
- Одновременно survived и был fetched `aussiecoinsandnotes.com`, то есть unknown retail/collector page.

Из этого следует:

- filter не просто "агрессивный"
- filter пока **неровный**
- он умеет отбрасывать явный мусор, но ещё не умеет стабильно сохранять лучшие кандидаты

### 2. Текущий budget fast-web не является "умным"

Лимиты сейчас заданы вручную:

- [config.py](/Users/yaroslavabramov/side/podgen/backend/app/config.py)
  - `max_fast_web_initial_gaps = 5`
  - `max_fast_web_verifier_gaps = 5`

Они:

- не завязаны на `duration_target_seconds`
- не завязаны на число вопросов в research tree
- не завязаны на число remaining verifier gaps
- не завязаны на размер claim pool

Для этого run это уже видно:

- verifier-loop selected `9` gaps
- processed only `5`

То есть это не "budget оказался достаточным", а просто hard stop.

### 3. Для длинных эпизодов эти лимиты по умолчанию не выглядят адекватными

Если выпуск длиннее и дерево вопросов глубже, текущий `5 + 5` scaling выглядит слишком грубо.

Следствие:

- pipeline может просто не попытаться закрыть значимую часть verifier gaps
- downstream quality будет ограничиваться не тем, что search ничего не нашёл, а тем, что система даже не попыталась часть gaps обработать

## Что это значит по смыслу

Нельзя делать вывод:

- "fast-web хорошо фильтрует страницы"

Корректнее:

- "fast-web уже умеет aggressively prefilter pages, но quality этой фильтрации пока ещё недостаточно надёжно коррелирует с source quality и relevance"

Нельзя делать вывод:

- "текущие budget caps разумны"

Корректнее:

- "текущие budget caps — это safety throttle, а не quality-aware planning"

## Что стоит поменять

### P0

- Ужесточить trust-aware page selection:
  - если в top candidates есть `official`, `legal`, `archive`, `academic` source с явным claim overlap, он не должен проигрывать unknown dealer/blog page
- Сделать debug dump богаче:
  - для `drop_low_value_page` писать reason / overlap / score, а не только action
- Явно разделить page budget:
  - `primary/high-trust fetch slots`
  - `fallback/secondary fetch slots`

### P1

- Сделать fast-web budgets адаптивными:
  - минимум учитывать `remaining_gap_count`
  - желательно учитывать `duration_target_seconds`
  - verifier-loop budget должен расти на более длинных и более насыщенных выпусках
- Вместо фиксированного `5` сделать policy уровня:
  - `min(base_cap, remaining_gaps)` для коротких выпусков
  - повышенный cap для более длинных выпусков

### P2

- Вынести budget policy из "магических констант" в явную стратегию:
  - short episode
  - standard episode
  - long episode
  - verifier-heavy retry case

## Практический вывод

По этому run fast-web уже приносит полезный материал, но:

- текущий page filter ещё нельзя считать надёжным quality gate
- текущие budget limits ещё нельзя считать адекватными по умолчанию, особенно для длинных эпизодов

Это не повод откатывать fast-web rework. Это повод считать следующими важными задачами:

- quality-aware page retention
- adaptive budget policy
