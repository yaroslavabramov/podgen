# WP-01: контракты и canonical storage

Python/Pydantic — единственный владелец 44 artifact kinds. PostgreSQL хранит immutable revisions, scoped связи, append-only допуски/отзывы и provenance. Blob adapters: локальное файловое хранилище и S3. HTTP, ModelGateway, production workflow и release assembly относятся к следующим WP.

## Воспроизводимый лабораторный запуск

Из корня репозитория:

```sh
uv sync --directory backend --locked
docker compose -p podgen-wp01 -f backend/compose.yaml up -d --build
export PODGEN_DATABASE_URL='postgresql+psycopg://podgen:podgen-test-only-password@127.0.0.1:55440/podgen_wp01'
export PODGEN_TEST_DATABASE_URL="$PODGEN_DATABASE_URL"
export PODGEN_TEST_S3_ENDPOINT='http://127.0.0.1:59091'
export PODGEN_TEST_MIGRATIONS=1
uv run --directory backend alembic upgrade head
uv run --directory backend pytest -q
uv run --directory backend ruff check src tests migrations
node --experimental-strip-types contracts/check-golden.ts
```

Compose собирает PostgreSQL 17 с pgvector 0.8.2. Базовый образ закреплён по digest, архив расширения проверяется по SHA-256. При обновлении Compose использует существующий том PostgreSQL. Нужны Node 22+, Docker и право создавать отдельную PostgreSQL test database. Миграционные испытания удаляют только собственную новую БД `podgen_migration_<uuid>`. Отсутствующие PostgreSQL/S3/migration параметры приводят к явным skips; такой запуск не доказывает соответствующую интеграцию.

Фактически использованные при разработке адреса: PostgreSQL `127.0.0.1:55439/podgen_wp01` (локальный кластер `/tmp/podgen-wp01/pgdata`, user `podgen`, test-only trust), MinIO `http://127.0.0.1:59090`. В этих командах достаточно заменить exports; compose использует другие порты.

## Синтетический GPS walkthrough

```sh
uv run --directory backend python -m tests.demo --out /tmp/podgen-wp01/gps-demo.json --objects /tmp/podgen-wp01/objects
```

Создаёт отдельный test project и полный набор сохранённых конфигураций, EpisodeSpec, пустого EditorialState, ResearchNote/DossierSnapshot/StoryPlan/ScriptRevision с двумя ведущими. JSON содержит полные envelopes. Это инженерный тестовый фрагмент, а не полный выпуск, экспертное досье или ready.

## Реальные материалы редакторского треда

```sh
uv run --directory backend python -m podgen.staging_cli ../evals/cases/development/wp02-batch-01 --new-test-project --objects /tmp/podgen-wp01/staging-objects --out /tmp/podgen-wp01/editorial-roundtrip.json
```

Весь набор путей и bytes должен совпасть с обязательным `inventory.sha256.json`; изменённый/лишний/пропущенный файл отклоняется **до публикации**. Скрипты пакета не исполняются. Прогон сохраняет и читает все bytes, полный текст досье/планов/четырёх сценарных файлов, порядок реплик и code-point offsets. Второй импорт идентичен; generation доступ к каждому `evaluator-only` файлу и общему evaluator index запрещён.

Staging — отдельные typed `user_input` records `editorial-staging-v1`, **без** DossierSnapshot/StoryPlan/ScriptRevision и acceptance. Автор — модель; неизвестные model ID и invocation archive остаются null. В отчёте перечислены необходимые mappings: политики/голоса/temporal scope; acquisition/extraction/rights; G/S → EvidenceSpan и original anchors; canonical dossier closure; план; стабильные script IDs и mapping правок; EvaluationAnnotation. Проверки смысла, независимого фактчека, аудио и человеческие approvals не выдумываются.

Для frozen b449d4d:

```sh
mkdir -p /tmp/podgen-wp01/editorial-b449d4d
git archive b449d4d evals/cases/development/wp02-batch-01 | tar -x -C /tmp/podgen-wp01/editorial-b449d4d
export PODGEN_TEST_EDITORIAL_ROOT='/tmp/podgen-wp01/editorial-b449d4d/evals/cases/development/wp02-batch-01'
uv run --directory backend pytest -q
```

Без `PODGEN_TEST_EDITORIAL_ROOT` тест проверяет текущую frozen партию. Во время законной редакторской правки старый inventory должен вызывать ошибку; после фиксации нового inventory повторяется весь прогон.

## Canonical import и схемы

```sh
uv run --directory backend python -m podgen.schema_export --out ../contracts/generated
uv run --directory backend python -m podgen.package_cli validate ../contracts/examples/research-note-package.json
uv run --directory backend python -m podgen.package_cli import PACKAGE.json --project UUID --episode UUID --actor UUID --objects /tmp/podgen-wp01/objects --out /tmp/podgen-wp01/imported.json
```

Существующий scope/ACL создаётся административной интеграцией, не моделью. `$artifact`/`$runtime`/`$blob`/`$text` — только шаблонные bindings импортёра, в canonical payload они заменяются exact refs/полным текстом. Единый DAG включает runtime и artifact nodes; циклы и дубли отклоняются. Runtime publication и artifacts одного пакета — одна транзакция. Blob upload может оставить orphan после неуспешного импорта; выдачи артефакта при этом нет.

Inline limit — 16 MiB; `create_from_stream` принимает полный JSON document до 256 MiB с явной ошибкой при превышении. Blob upload сохраняет полный stream. JSON columns сохраняют также U+0000, который JSONB не поддерживает. Нет Unicode normalization и усечения.

## Доступ и current quality

- Catalog — exact published configuration, только оператор; private/custom voices не публикуются. `resolve_for_cell` проверяет **равенство** companion combination. Accepted qualification требует exact CapabilityReport **и** passed EvaluationResult на совпавшей EvaluationCase cell и измеренных ролях. Интервалы длительностей до появления calibrated policy не расширяются из одного случая.
- Parent/provenance не равен active quality dependencies. Accepted owner disposition и его authority снимают только соответствующий текущий impact. `retained` действует только на уже проверенные quality events; новый correction снова блокирует. Отзыв authority возвращает консервативные edges.
- Privacy следует всей provenance, runtime связям, старым alias owners и производным blobs. Append-only hash tombstones запрещают повторное присоединение/чтение и новый upload тех же bytes **в этом проекте**, включая иной media type и published audition. Quality review не снимает этот запрет. Автоматического regrant нет.
- Закрытые bytes отдельно помечаются evaluator-only по hash внутри проекта. Generation не читает и не перепривязывает их через ArtifactRef, RuntimeRef, старую обёртку или повторный upload; evaluation-доступ требует текущей роли. Классификация распространяется на производные bytes, но не вверх на авторские источники, которые gold лишь цитирует. Публикация catalog audition не снимает классификацию автоматически.
- Потерянный ответ integrate/revalidate повторяется без второй mutation; другой payload под тем же ID — conflict. Текущие права и fencing проверяются и на replay.
- TaskRun имеет собственный manifest head. Чужая промежуточная ветка и замена model/prompt/toolset обычным admission отклоняются.

Физический purge, orphan reconciliation, IAM/credential deployment, durable worker recovery, production authentication/API и model reviewer qualification относятся к WP-03/05/09/12. Здесь проверена доступность через repository; прямой административный доступ к DB/bucket не является пользовательским read path.

## Запуск автора

Работающий OpenAI evaluation CLI, явные profiles, draft → review → revise, полный архив и UTF-8 export: [WRITING-EVALUATION.md](WRITING-EVALUATION.md). Это отдельный ограниченный evaluation этап без production acceptance.

## Поиск по источникам

Новые production-выпуски используют локальные multilingual encoder/reranker и project-scoped PostgreSQL FTS/pgvector. После установки зависимостей и миграций подготовьте общие модели: `uv run --directory backend python -m podgen.retrieval prepare` из корня репозитория. [Установка, pins, обычный run/preview/resume и адресные проверки](../docs/podcast-production/source-search.md).

Обычный `resume` восстанавливает технически оборвавшуюся стадию в том же запуске: сначала получает известный ответ, затем при необходимости создаёт связанную попытку. Максимум две технические замены на стадию; сохранённые тексты, рецензии, чтения источников и неизвестный расход сохраняются. [Контракт и проверки восстановления](../docs/podcast-production/technical-recovery.md).

После `revision_limit_exhausted` штатный `continue-editorial` продолжает последнее поручение с явным дополнительным бюджетом и общим потолком трёх переписей. [Контракт, preview и учёт истории](../docs/podcast-production/editorial-continuation.md).
