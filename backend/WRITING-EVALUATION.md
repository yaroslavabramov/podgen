# Авторская генерация: компактные ролевые входы

Новый рабочий путь — `editorial-role-packets/1` и `review_mode=independent-readings/1` по умолчанию в CLI и Python. Каждый полный текст получает два отдельных чтения Terra: слушательское без плана/брифа/источников и фактологическое по материалам и factual remit. Второе не видит первого заключения. Sol получает actionable замечания обеих ролей с происхождением и выборочные опоры для правки. Полные исходники и отзывы остаются в архиве. Проекция `/4` не задаёт актам обязательных списков оговорок: автор получает ход понимания, обоснованный ответ, опоры с условиями и временной диапазон. Расчётные assumptions, optional_material и общие списки ограничений не становятся заданиями на озвучку; точные findings/preserve и фактические опоры правки сохраняются.

Общий итог: необходимые материалы → обязательная правка → keep. Третьего модельного арбитра нет. После каждой переписи оба чтения повторяются для точной новой версии. `--max-script-revisions 0|1|2` ограничивает число переписей (default 2). Исчерпание лимита и `needs_material` сохраняют отказ и последний кандидат; одобренный `final` не создаётся.

Это независимые модельные проверки evaluation-кандидатов, без production acceptance, аудио и подтверждения человеческого прослушивания. Прежний одиночный review сохранён как исторический `informed-v1`.

## Подготовка и admission без модели

Установите зависимости через `uv sync --directory backend --locked`. Используйте подготовленную изолированную БД с миграциями до `0015_independent_readings`; не мигрируйте базу активных запусков. Задайте `PODGEN_DATABASE_URL`, `PROJECT_UUID`, `ACTOR_UUID` и абсолютный `OBJECTS` на существующее хранилище проекта.

Общий префикс для следующих команд:

```sh
pgwrite() {
  uv run --directory backend python -m podgen.writing_cli \
    --project "$PROJECT_UUID" --actor "$ACTOR_UUID" --objects "$OBJECTS" "$@"
}
```

`/tmp/source-map.json` — только сопоставление `source_path → точный RuntimeRef/SourceRepresentation ArtifactRef`, например сохранённый `file_refs` проверенного staging import. Полный staging report или старый author root вместо shared ref не принимается.

```sh
pgwrite editorial-import \
  --input ../evals/editorial-inputs/v1/gps-clock/shared.json \
  --source-map /tmp/source-map.json --out /tmp/gps-shared-ref.json

pgwrite fixed-editorial-import \
  --input ../evals/editorial-inputs/v1/gps-clock/fixed-plan.json \
  --source-map /tmp/source-map.json --shared-ref @/tmp/gps-shared-ref.json \
  --out /tmp/gps-fixed-ref.json
```

Для нового назначения те же схемы поддерживают произвольную тему, `solo`/`dialogue`, язык, голоса и их функции. Новые необязательные `assignment.listener_profile` и `assignment.genre` содержат только нейтральное описание слушателя и жанра. Холодное чтение не получает прежние audience/style_guidance; при отсутствии безопасных полей используются нейтральный слушатель без знания темы и жанр podcast. Новые точные документы можно отдельно принять через `editorial-source-import --file /ABS/SOURCE.txt --source-path sources/source.txt --scope ASSIGNMENT_SCOPE --purpose primary --out /tmp/source-ref.json`; доступные назначения также `brief`, `dossier`, `fixed_plan`.

Импорт сверяет полный исходник, hash, происхождение и область источников, строгие Unicode codepoint диапазоны `[start,end)` и текст каждой цитаты. Повтор и одновременный импорт одинаковой версии идемпотентны. Оригинальные JSON bytes сохраняются; слишком большой модельный вход вызывает ошибку, а не усечение. Общий пакет не содержит ссылок на fixed-plan; отдельный fixed пакет связан с точным digest shared.

Схемы, пример JSON и варианты preview: [schemas/README.md](schemas/README.md).

## Профили и offline preview

`profile-create` только сохраняет candidate configuration, без вызова модели и qualification. Существующие подходящие writer/reviewer profiles можно использовать повторно: writer — Sol, reviewer — Terra для обоих независимых чтений. Указывайте точные профили выбранных моделей. Ниже пример закреплённого writer/planner snapshot; для reviewer задайте точные REVIEWER_MODEL и REVIEWER_CONTEXT_LIMIT выбранного профиля:

```sh
pgwrite profile-create --model gpt-5.4-2026-03-05 --context-limit 1050000 \
  --output-limit 32768 --reasoning-effort medium --verbosity high \
  --out /tmp/writer-profile-ref.json

pgwrite profile-create --model gpt-5.4-2026-03-05 --context-limit 1050000 \
  --output-limit 32768 --reasoning-effort medium --roles planner plan_reviewer \
  --out /tmp/planning-profile-ref.json

pgwrite profile-create --model "$REVIEWER_MODEL" --context-limit "$REVIEWER_CONTEXT_LIMIT" \
  --output-limit 32768 --reasoning-effort medium --roles editorial_reviewer \
  --out /tmp/reviewer-profile-ref.json
```

По умолчанию `store=false`, `background=false`; хранение у провайдера и background retrieval требуют явных `--store --background`. Роли, входы и вызовы независимы; профили закрепляются в manifest.

Preview использует локальные БД и хранилище. API-ключ, клиент провайдера и token-count endpoint не нужны:

```sh
pgwrite preview --run-id "$PREVIEW_UUID" --case gps-clock --stage draft \
  --shared-ref @/tmp/gps-shared-ref.json --fixed-ref @/tmp/gps-fixed-ref.json \
  --profile @/tmp/writer-profile-ref.json --out /tmp/gps-draft-preview.json

pgwrite preview --run-id "$PREVIEW_UUID" --case gps-clock --stage plan \
  --shared-ref @/tmp/gps-shared-ref.json --profile @/tmp/planning-profile-ref.json \
  --out /tmp/gps-plan-preview.json
```

В файле — точный request и размеры `instructions`, `input`, `schema`. При превышении byte limit файл всё равно сохраняется, команда возвращает validation error. Для следующих ролей передаются чистые `PlanningOutput`, `WriteOutput`, `EditorialFeedback`, без архивной обёртки `output`: `--candidate`, `--plan-review`, `--approved-plan`, `--draft`, `--feedback` по задаче роли. Поддерживаются preview отдельных `anchor-repair` и `plan-target-repair`.

## Готовый сохранённый сценарий

Импорт принимает чистый полный `WriteOutput` (utterances), без архивной обёртки. Сохраняются точные bytes и отдельный смысловой hash; разная JSON-разметка не конфликтует.

```sh
pgwrite script-import --file /ABS/snow-script.json --out /tmp/snow-script-ref.json

pgwrite run --run-id "$SAVED_SCRIPT_RUN_UUID" --case snow-pump \
  --author-ref @/tmp/snow-shared-ref.json --initial-script-ref @/tmp/snow-script-ref.json \
  --writer-profile @/tmp/writer-profile-ref.json --reviewer-profile @/tmp/reviewer-profile-ref.json \
  --max-script-revisions 2 --review-anchor-policy bounded-selection-v1 \
  --export-dir /tmp/snow-independent-readings
```

Новый план и draft не генерируются; fixed ref не нужен. Вместо импортированного ref допустим точный RuntimeRef существующего сохранённого draft/final. Для проверки входов без сети:

```sh
pgwrite preview --run-id "$PREVIEW_UUID" --case snow-pump --stage reader-review \
  --shared-ref @/tmp/snow-shared-ref.json --draft /ABS/snow-script.json \
  --profile @/tmp/reviewer-profile-ref.json --out /tmp/snow-reader-preview.json

pgwrite preview --run-id "$PREVIEW_UUID" --case snow-pump --stage fact-review \
  --shared-ref @/tmp/snow-shared-ref.json --draft /ABS/snow-script.json \
  --profile @/tmp/reviewer-profile-ref.json --out /tmp/snow-fact-preview.json
```

Wire и preview правки описаны в [schemas/independent-readings.md](schemas/independent-readings.md).

## Новый сетевой run

Только `run`/продолжение нового компактного run выполняют сетевые вызовы. `OPENAI_API_KEY` передаётся окружением; ключ не входит в profile, prompt или архив headers. Каждый независимый run получает новый UUID.

Фиксированный редакторский план:

```sh
pgwrite run --run-id "$NEW_RUN_UUID" --case gps-clock \
  --author-ref @/tmp/gps-shared-ref.json --fixed-plan-input-ref @/tmp/gps-fixed-ref.json \
  --writer-profile @/tmp/writer-profile-ref.json --reviewer-profile @/tmp/reviewer-profile-ref.json \
  --export-dir /tmp/gps-compact-run
```

Собственный план, без fixed ref:

```sh
pgwrite run --run-id "$NEW_OWN_RUN_UUID" --case gps-clock \
  --author-ref @/tmp/gps-shared-ref.json \
  --writer-profile @/tmp/writer-profile-ref.json --reviewer-profile @/tmp/reviewer-profile-ref.json \
  --plan-mode own-plan --planner-profile @/tmp/planning-profile-ref.json \
  --plan-reviewer-profile @/tmp/planning-profile-ref.json --max-plan-revisions 1 \
  --export-dir /tmp/gps-compact-own-run
```

Для Snow используются его shared/fixed packets и новые UUID/папки. Собственный план проходит `plan → plan-review`, при необходимости одну полную правку и повторное review. Только `accept` точной ready-версии без blocking research допускает автора. Автор не получает указание «создай собственный план», похвалу, решение планового редактора или служебные связи.

Границы новых запусков:

- Не более 128 КиБ суммарного JSON представления видимых `instructions/input/schema`, проверка до создания клиента и count.
- Не более 32 768 входных токенов на вызов, проверка после provider count и до submit.
- Defaults на весь run: 196 608 входных и 131 072 выходных токена, 7200 секунд. Можно указать меньшие `--max-total-input-tokens`/`--max-total-output-tokens`/`--max-elapsed-seconds`.
- Перед submit полностью резервируется `profile.output_limit`; также проверяется `input + reserved output ≤ context_limit`. Длительность/полнота сценария не сокращаются автоматически.

На локальной проекции реальных сложных архивных планов максимальный видимый запрос: GPS 73 792 байта, Snow 106 662; автор по собственному плану — 43 846/55 584. Таблица всех ролей и оговорка об адресном сопоставлении диагностических старых опор — в schemas README. Это размеры локального preview, не расход нового сетевого запуска.

## Inspect, финал и resume

```sh
pgwrite inspect --run-id "$RUN_UUID" --export-dir /tmp/run-inspect
pgwrite resume --run-id "$COMPACT_RUN_UUID" --export-dir /tmp/run-resumed
```

Архив содержит точные prompts/versions, schemas, profiles, request intent, реальные wire/response bytes, IDs, usage, ошибки и все попытки. `inputs.json` сохраняет полный backend audit; оригинальные редакторские JSON экспортируются отдельно. Перед count/submit живые права на исходники проверяются заново. Отзыв источника закрывает зависимые candidates, decisions, request/response archives и clearance; evaluator archive не даёт бессрочного права на повторную отправку.

Сначала читайте `review-result.md`: итог, оба чтения каждой версии, замечания, оговорки и вопросы исследователю. Машинный итог — `review-outcome.json`; точные привязки каждой пары — `reading-round-N.json`. Полные `reader-review[-N].json`, `fact-review[-N].json`, `revise-N.json/txt` и все попытки сохраняются отдельно. Только keep после обоих чтений этой версии создаёт `final.json/txt`. При отказе остаётся `candidate-final.json/txt`; код CLI равен 2, а `review_status` — needs_material либо revision_limit_exhausted. Код 0 означает модельный keep, без допуска к публикации. Завершённый resume возвращает тот же итог, не создаёт клиента и не запускает цикл заново.

Экспортная папка закрепляется маркером за project/run. Чужая непустая папка, другой run и symlinks отклоняются. Старый экспорт без маркера читается в новую пустую папку.

После submit intent с неизвестным исходом повторная генерация запрещена. Известный response ID и разрешённое provider storage допускают retrieve; сохранённый completed response разбирается повторно без submit. SDK transport retries отключены. Snapshot, response model, metadata и retrieve response ID сверяются. Из headers хранится только `x-request-id`; обнаруженный echo API key редактируется с явной отметкой.

## Ограниченные адресные repairs и отказ по балансу

Новый run по умолчанию использует strict anchors. `--review-anchor-policy bounded-selection-v1` заранее разрешает максимум один отдельный selector-call на каждую роль и версию текста после сохранённой ошибки якорей. Режим доступа и суждения сохраняются. Для остановленного компактного run доступны явные действия:

```sh
pgwrite resume --run-id "$COMPACT_RUN_UUID" --repair-review-anchors --export-dir /tmp/anchor-repair
pgwrite resume --run-id "$COMPACT_RUN_UUID" --repair-plan-targets "$FAILED_REVIEW_INVOCATION_UUID" --export-dir /tmp/plan-target-repair
pgwrite resume --run-id "$COMPACT_RUN_UUID" --retry-credit-refusal "$FAILED_INVOCATION_UUID" --export-dir /tmp/credit-retry
```

Anchor repair получает только невалидные anchors, их суждения и точные диапазоны релевантных реплик. Сервер принимает только IDs, предложенные конкретному anchor; `null`, неизвестный, пропущенный, повторный или существующий, но не предложенный ID останавливает цепочку. Суждения и валидные anchors сохраняются. Реальный Snow: 29 диапазонов вместо 192, payload 14 572 байта на три замечания.

Plan-target repair получает один точный candidate, его существующие JSON Pointers и только невалидные targets с контекстом суждений. Полный корпус и исходный review request не копируются. Модель выбирает адрес или `null`; сервер не угадывает targets, не редактирует вывод и не назначает accept. Сохраняются исходное решение, все judgments/research, валидные адреса и полный raw rejected response. На каждый plan-review этап — максимум одна смысловая попытка.

Credit retry допускается только для архивного submit error HTTP 429 с `credit_balance_exhausted`, `ambiguous=false`, без response ID/ответа/output. Успехи, count errors, прочие HTTP/validation faults и неизвестный исход не допускаются. UUID выбирается из `run.json → invocations → attempts`. Одна команда разрешает одну новую физическую попытку; повтор команды идемпотентен. Новый подтверждённый отказ требует явной команды с новым UUID.

Оплаченные стадии не создаются заново. Credit retry сохраняет прежние exact-wire bytes кроме metadata привязки попытки; бюджет и время не начинаются заново. Preflight counts учитываются консервативно; доказанный отказ без генерации не резервирует output. `attempt_kind` и `operation_kind` различают role, semantic repair и physical credit retry. Все lineage, receipts и попытки экспортируются; repair не даёт квалификации или acceptance.

## Исторический компактный цикл `informed-v1`

Явный `--review-mode informed-v1` сохраняет прежний `draft → review → revise`: один informed review с плановым ориентиром, без независимого холодного чтения и повторного чтения переписанного финала. Manifest без review_mode возобновляется именно так; сохранённые prompts, schemas, requests и fingerprints не заменяются текущими.

При обычной правке экспортируются полные `draft.*`, `review.json`, `revise.*`. При `keep` — также `final.json`/`final.txt`; совместимый `revise.*` явно имеет `origin=unchanged_draft`, `model_call=false`, ссылки на draft и review, без фиктивного invocation. В `archive/revise/` запроса нет. Завершённый replay не создаёт клиента и не проверяет новый бюджет.

## Исторический raw baseline: только архив и offline replay

Старые manifest, prompts, schema, лимиты и wire bytes остаются неизменными. Полный `AuthorMaterials` на каждой стадии и прежние повышенные лимиты относятся только к этим архивным условиям.

`inspect` и завершённый replay бесплатны. Незавершённый raw `resume`, включая repair/credit retry, штатно останавливается **до создания клиента** с `legacy_outbound_disabled_start_compact_run`. Для новой генерации создайте компактный run после admission/preview. Флага отправки raw через новый CLI `run` нет. Исторический exact-wire compiler доступен локальным тестам только через явно внедрённый `legacy_offline_transport`, отдельно от сетевой фабрики.

Уже сохранённые numbered instructions `/1`, `/2`, `/3`, старые selector maps и fingerprints не пересобираются. Исторические UUID/reference selectors остаются backend данными.

## Проверки и границы

```sh
PODGEN_TEST_DATABASE_URL="$ISOLATED_TEST_DATABASE_URL" \
  uv run --directory backend pytest -q
uv run --directory backend ruff check src tests
```

Тесты используют настоящий PostgreSQL/ACL/journal, локальные provider doubles и HTTP mock transport. API/платные запуски для этой проверки не нужны. Покрыты admission обеих реальных тем, новые задания, exact excerpts/original bytes, одновременный импорт, own/fixed isolation, бюджеты до клиента/submit, repairs, keep finalization, ordinary CLI, установленный пакет и исторический replay.

Публикация архива выполняется в короткой repeatable-read write-транзакции с memo на одну проверку графа. Блокировки project/member сериализуют её с отзывом доступа; повторяется только откатившаяся SQL-транзакция `40001`, максимум три раза, без provider calls внутри callback. Read snapshots/memo не переиспользуются между публичными чтениями, проектами или purposes; циклы проверяет отдельный active-path guard.

Открыты canonical StoryPlan/ScriptRevision mapping, эмпирическая проверка слушательского понимания, производственная квалификация фактчекинга/провайдера и остальная матрица. Независимые модельные чтения реализованы; runtime records не подменяют эти допуски.
