# Авторская генерация: baseline A

Рабочий evaluation-путь: полный frozen input → целый draft → отдельный informed editorial review → полная новая версия. Три самостоятельных Responses API вызова, без conversation carry-over, web/tools/filesystem. Один и тот же полный `AuthorMaterials.as_payload()` используется на всех стадиях. Исходные материалы загружает generation repository; runner не читает staging evaluator index, gold или прежние сценарии.

Это baseline A на непринятом frozen writer input, **не** независимый EXP-04, фактчек, qualification, canonical ScriptRevision или production release. Результаты — явно помеченные `writing-evaluation-output-v1` runtime records, без AcceptanceRecord.

## Подготовка

```sh
uv sync --directory backend --locked
export PODGEN_DATABASE_URL='postgresql+psycopg://podgen:podgen-test-only-password@127.0.0.1:55440/podgen_wp01'
uv run --directory backend alembic upgrade head
```

Миграции `0010_writing_evaluation` и `0011_review_anchor_attempt`. Не применять миграции к базе под активными live runs. `OPENAI_API_KEY` передаётся окружением либо через глобальный `--env-file ../.env`; ключ не входит в profile, prompt или архив headers. `--objects` должен указывать **тот же** каталог, где находятся blobs исходного staging.

`--project`/`--actor` и отдельный author RuntimeRef берутся из assignments. Нельзя передавать полный staging report вместо ref. Оператор может сохранить только `report.author_inputs[case]` в отдельный JSON-файл; runner прочтёт именно его.

## Явный candidate profile

Пример для проверенного snapshot; все настройки сохраняются в immutable ModelProfile:

```sh
uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  profile-create --model gpt-5.4-2026-03-05 --context-limit 1050000 \
  --output-limit 32768 --reasoning-effort medium --verbosity high --timeout-seconds 1800 \
  --out /tmp/writer-profile-ref.json
```

Без `--store` и `--background` оба значения false. Для сохранения provider response и polling с известным response ID **явно** добавляются `--store --background`. Квалификация не присваивается. Для reviewer можно создать другой профиль; если выбран тот же ref, независимы вызовы/роли, а не модель/провайдер.

Контекст GPT-5.4 — 1 050 000, max output — 128 000 по [официальной модели](https://developers.openai.com/api/docs/models/gpt-5.4). Авторский корпус GPS/Snow занимает примерно 150–200k input tokens до draft/review. Output limit включает reasoning. Окно не сокращается ради цены: перед каждым submit выполняется [официальный input token count](https://developers.openai.com/api/docs/guides/token-counting). При `input + reserved output > context_limit` — явная ошибка без генерации и усечения.

## Запуск обоих кейсов

Для каждой независимой попытки нужен **новый UUID**. `repeat_index` — метка повтора, не ключ result cache.

```sh
uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  run --run-id NEW_RUN_UUID --case gps-clock --author-ref @/tmp/gps-author-ref.json \
  --writer-profile @/tmp/writer-profile-ref.json --reviewer-profile @/tmp/writer-profile-ref.json \
  --repeat-index 0 --max-total-input-tokens 900000 --max-total-output-tokens 196608 \
  --max-elapsed-seconds 7200 --export-dir /tmp/writing-gps-NEW_RUN_UUID
```

Для Snow: `--case snow-pump`, свой author ref, **другой** run UUID и export-dir. Полные документы, draft и feedback не сокращаются. Review получает явный `index` у каждой реплики. `EditorialFeedback.validate_against` проверяет zero-based indices и exact quotes; в strict-режиме невалидный feedback сохраняется в raw response, фиксируется конкретная ошибка, revision не вызывается. Никакого эвристического подбора цитат или скрытого repair-вызова.

Сохраняются actual role instructions/version, JSON schemas, exact profile refs/settings, request intent, реальные wire JSON bodies и response bodies, provider/request IDs, usage, ошибки и outcome_unknown. Headers целиком не сохраняются: только `x-request-id`. Echo API key в error body редактируется с явным флагом. SDK transport retries отключены; model snapshot и возвращённый `response.model` обязаны совпасть, metadata и retrieve response ID проверяются до принятия текста.

## Inspect и resume

```sh
uv run --directory backend python -m podgen.writing_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  inspect --run-id RUN_UUID --export-dir /tmp/NEW_EMPTY_EXPORT_DIR

uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  resume --run-id RUN_UUID --export-dir /tmp/NEW_EMPTY_EXPORT_DIR
```

Экспорт: `draft.txt`, `revise.txt` (speaker-labeled UTF-8), полные `draft.json`/`review.json`/`revise.json`, `manifest.json`, `inputs.json`, `run.json`, `archive/<stage>/request.json`, events и raw provider bodies. Скрытые поля feedback сохраняются целиком; AcceptanceRecord не создаётся. Папка закрепляется маркером за одним project/run. Чужая непустая папка и другой run отклоняются. Для legacy экспорта без маркера выберите новую пустую папку.

Завершённый run возвращается без нового бюджета, компиляции или provider calls после перепроверки принадлежности, fingerprint и живых прав на исходные refs. Частичный run использует архивированные PromptDefinition; новые инструкции предназначены только новым run IDs. Перед каждой фактической отправкой исходные refs снова проверяются через generation scope, в том числе после token count. Архив evaluator request не даёт вечного права пересылать отозванный источник.

После persisted submit intent без подтверждения/сохранённого ответа повторная генерация **запрещена** (`outcome_unknown`). Известный response ID и разрешённое provider storage допускают retrieve; уже записанный completed response повторно разбирается без submit. Если нет подтверждённого ID/ответа, автоматического reconciliation нет: сначала операторская проверка провайдера. HTTP/validation/refusal/incomplete ошибки не скрываются новым запуском. Новый run — новая независимая попытка; все старые данные остаются.

## Проверки и границы

```sh
PODGEN_TEST_DATABASE_URL='postgresql+psycopg://podgen@127.0.0.1:55439/podgen_wp01' \
  uv run --directory backend pytest -q tests/test_writing_evaluation.py tests/test_review_anchor_recovery.py tests/test_openai_provider.py tests/test_writing_cli.py tests/test_writing_migrations.py
```

Синтетические provider faults используют настоящий PostgreSQL и настоящий OpenAI SDK с HTTP mock transport. CLI-тест проходит оба реальных frozen author inputs целиком, но не вызывает live API. Автор этого изменения live episodes не дублировал; внешние реальные попытки запускает/reviews CTO/CPO отдельно.

Открыты: полные WP-02/03 (controlled corpus, typed tool runtime, ReadReceipt/TaskRun integration, provider qualification/reconciliation), WP-05 (независимый factcheck/coverage/cold reading), остальной WP-06 (canonical plan/script mapping, continuity/acts/research loop), WP-07–14. Нет production approvals, аудио, human listener assessments, универсального восстановления неизвестного provider outcome или квалифицированной поддержки всей продуктовой матрицы.

## Ограниченное исправление review anchors

Новые запуски по умолчанию используют `--review-anchor-policy strict`: ошибка exact anchor останавливает цепочку. Явный `run --review-anchor-policy bounded-selection-v1` сохраняет политику в manifest/fingerprint и автоматически выполняет максимум один дополнительный selector-вызов после сохранённой ошибки review. Все исходные token/time/context limits остаются прежними; стоимость неудачного review входит в общий бюджет.

Для уже остановленного строгого запуска:

```sh
uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  resume --run-id RUN_UUID --repair-review-anchors --export-dir /tmp/NEW_EMPTY_EXPORT_DIR
```

Нужна миграция `0011_review_anchor_attempt`. Legacy manifest без policy читается как strict с прежним fingerprint. Явное восстановление фиксируется отдельным immutable событием, не переписывает исходный manifest и не создаёт новый draft. Сохранённый полный failed review, его request/response и usage остаются в `archive/review/`; дополнительная попытка — `archive/review/attempt-1/`. `run.json` содержит обе попытки и суммарный известный usage.

Selector получает исходные материалы, draft и полный отклонённый feedback. Он выбирает только серверные IDs точных фрагментов для невалидных anchors. Сервер восстанавливает цитаты из draft; суждения и уже валидные anchors не меняются. `null`, неизвестные/пропущенные/повторные IDs останавливают цепочку без revise. Неоднозначный submit, malformed JSON, refusal/incomplete и прочие ошибки не получают такого retry. Повторный resume не создаёт второй repair; каждая отправка снова проверяет доступ к исходникам. Это дополнительное экспериментальное условие, не квалификация и не semantic acceptance.


## Own-plan: свой план с отдельной проверкой

`--plan-mode fixed-plan` — прежнее условие по умолчанию; прежние команды, fingerprints, сохранённые prompts и replay совместимы. `--plan-mode own-plan` использует полный бриф, досье и те же проверенные первичные представления источников, но исключает предоставленный `author/.../plan.md`, gold и прежние сценарии на всех этапах. Указания неизменённого брифа о предоставленном плане явно помечаются в payload как относящиеся только к fixed-plan.

Нужна миграция `0012_own_plan_stages`; старые записи не изменяются. Планирование: `plan → plan-review`; при `revise` — максимум одна `plan-revise → plan-review-revised`. Ограничение явно задаётся `--max-plan-revisions 0|1` (по умолчанию 1). Только `accept` для точной сохранённой версии без blocking research даёт её автору `draft → review → revise`. Вопросы с `blocking=false` не требуют исследования автоматически. Исчерпанная правка и отсутствие материала завершаются `needs_attention`; исследователь не имитируется. Отзыв разрешённого источника закрывает сохранённые requests, raw responses, candidates, decisions и clearance через WP-01 provenance.

Создайте отдельный **candidate** профиль с planning-ролями; существующие writer/reviewer refs менять не требуется:

```sh
uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  profile-create --model gpt-5.4-2026-03-05 --context-limit 1050000 \
  --output-limit 32768 --reasoning-effort medium --roles planner plan_reviewer \
  --out /tmp/planning-profile.json

uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  run --run-id NEW_UUID --case gps-clock --author-ref @/tmp/gps-author-ref.json \
  --writer-profile @/tmp/writer-profile-ref.json --reviewer-profile @/tmp/writer-profile-ref.json \
  --plan-mode own-plan --planner-profile @/tmp/planning-profile.json \
  --plan-reviewer-profile @/tmp/planning-profile.json --max-plan-revisions 1 \
  --review-anchor-policy bounded-selection-v1 --max-total-input-tokens 2100000 \
  --max-total-output-tokens 327680 --max-elapsed-seconds 7200 --export-dir /tmp/own-gps-NEW_UUID
```

Для Snow замените case, author ref, UUID и папку. Все planning-вызовы входят в общий token/time budget; settings и repeat_index не подменяются автоматически. `resume` и `inspect` прежние. Ambiguous submit не отправляется повторно.

Модель получает явный `material_catalog`: точные labels из единственного разрешённого `evidence.json` темы и пути реально переданных документов. Labels связаны с проверенными полными представлениями и selectors. Review использует JSON Pointers к точному `PlanningOutput`. UUID кандидата назначает backend; JSON содержит ссылки на предыдущую версию, её hash и отдельное решение. Own-plan хранит широкий staging root как **селектор**, а зависимости manifest — как явные refs только выбранных документов. Это предотвращает обход через broad root или aliases и не делает неиспользованный fixed-plan источником полномочий.

Экспорт включает полные `plan.json`, `plan-review.json`, при правке `plan-revise.json`/`plan-review-revised.json`, отдельный `planning-clearance.json` и полный архив каждого вызова. `generated-plan.md` показывает именно проверенную версию; при блоке — явно непринятый кандидат и конкретные вопросы/причины. Все эти результаты — evaluation-кандидаты, не принятый производственный StoryPlan и не независимый фактчек.

Repository повторно использует успешно проверенные узлы только внутри одного публичного чтения в PostgreSQL read-only repeatable-read snapshot. Конкурирующий отзыв применяется к следующему чтению; snapshot и memo не живут между вызовами, транзакциями, проектами или purposes. Guard перед count/submit открывает новые чтения и снова проверяет живой доступ. Циклы проверяются отдельным active-path guard.

Для `journal.record/create_run/prepare/event` используется отдельная короткая write-транзакция с repeatable-read снимком и memo на одну проверку графа. `FOR SHARE` проекта сериализует публикацию с обычным `DependencyLedger` access withdrawal, а блокировка строки членства — с удалением/понижением роли. Отзыв после публикации видит и новые рёбра/производные bytes; отзыв, выигравший гонку, приводит к новому снимку и отказу в записи. Повторяется только откатившаяся SQL-транзакция с `40001`, максимум три раза; provider calls внутри callback отсутствуют. Общая политика остальных write-транзакций не менялась.


## Явное продолжение после отказа по балансу

Требуется `0013_credit_refusal_attempts`. Обычный `resume` не повторяет ошибку. Оператор может разрешить **одну** новую физическую попытку конкретного отказа:

```sh
uv run --directory backend python -m podgen.writing_cli \
  --env-file ../.env --project PROJECT_UUID --actor ACTOR_UUID --objects /ABS/STAGING_OBJECTS \
  resume --run-id RUN_UUID --retry-credit-refusal FAILED_INVOCATION_UUID \
  --export-dir /tmp/RECOVERED_RUN_EXPORT
```

Допускается только сохранённый submit error: HTTP 429, `error.code=credit_balance_exhausted`, `ambiguous=false`, без response ID, сохранённого ответа или output. Проверяются status, submit intent, связанный request и полный raw error. Успехи, count errors, прочие HTTP/validation faults и неизвестный outcome не допускаются. Команда объясняет refusal и неизвестный outcome разными кодами/сообщениями; ошибочный UUID не меняет status/failure_code run.

UUID берётся из `run.json` → `invocations` → нужный этап → `attempts`. Повтор той же команды использует уже созданную попытку. Если новая попытка опять получила отказ по балансу, необходима **новая команда с её UUID**. Подтверждение и receipt сохраняются атомарно в request record; новый UUID детерминирован по родителю. Crash после submit intent не разрешает повторный submit.

Оплаченные plan/draft/review не создаются заново. Сохраняются исходный manifest, prompts/model/schema, полные input/instructions и исходные request/wire/response bytes. У нового запроса меняется только metadata привязки к новой попытке. Проверяются прежние права и бюджеты, время run не начинается заново. Предыдущие preflight input counts остаются в консервативном общем input budget; у доказанного отказа без генерации нет output reservation. Это не подмена сохранённых usage: исходные поля и raw receipts остаются неизменными.

`attempt_kind` различает `role`, один `anchor_repair` и явный `credit_retry`; `operation_kind` сохраняет исходную работу. Поэтому retry отказавшего anchor repair не создаёт второе смысловое исправление. Все предыдущие попытки экспортируются: первая в `archive/<stage>/`, остальные в `archive/<stage>/attempt-N/`; `run.json` перечисляет lineage, statuses и суммарные известные usage/counts. `resume --retry-credit-refusal ...` не включает repair policy молча: исходная policy run и явный `--repair-review-anchors` действуют как прежде.
