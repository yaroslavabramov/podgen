# Текущий Snow: пользовательский заказ

[Задание и досье](shared.json) · [точный исходный бриф текущей версии](brief.md).

Для нового выпуска использовать `shared.json` из этой папки и штатный `--plan-mode own-plan`. Планировщик выбирает начало и маршрут, отдельный редактор проверяет план; затем идут автор, редактор сценария и правка при необходимости. Фиксированного плана у этой версии нет.

В [brief.md](brief.md) — только дословный пользовательский запрос. Тот же текст хранится в `assignment.question`: имя поля не означает, что пользователь задал готовый центральный вопрос. Здесь заказаны тема и детективная подача; интересный центральный вопрос планировщик находит в источниках самостоятельно. `listener_outcome` отражает только этот замысел. В остальных полях задания нет подготовленного сюжета; `required_topics` и `style_guidance` пусты. Досье сохранено целиком и служит запасом материала.

Отдельные настройки теста в shared: русский язык, один рассказчик, цель 15 минут с диапазоном 13–18 минут, взрослый неспециалист, впервые слышащий эту историю. Они не являются частью дословного запроса. Жанр «Детектив» взят из просьбы пользователя. Читатель получает текст и нейтральные метаданные, фактчек — также фактический заказ и источники. Начало, порядок и речевую форму выбирают планировщик и автор.

В промежуточной пробе 6 ноября `opening.approach` модели начинался словами «Сохранить заголовок „Зачем закрыли колонку на Брод-стрит, если холера уже отступала?“». Это был редакционный рабочий тайтл, не требование пользователя; он сохранял влияние прежнего контроля. В той версии название заменили на нейтральное «Джон Сноу и вспышка холеры в Лондоне» и обновили SHA брифа, сохранив тогдашний вопрос и жанровые указания. Нынешняя версия отдельно заменяет навязанные вопросы дословным пользовательским запросом. Исходные пробы и их расходы учитываются отдельно; старые архивы не заменяются новой версией.

Все 16 карточек, досье и первичные выдержки с точными диапазонами перенесены из v1 без изменений. Новый бриф — отдельный источник с новым хешем. Остальные три документа имеют прежние байты и хеши. Их расположение для допуска:

| `source_path` в пакете | Файл от корня репозитория | Назначение |
| --- | --- | --- |
| `editorial-inputs/current/snow-pump/brief.md` | `evals/editorial-inputs/current/snow-pump/brief.md` | `brief` |
| `author/snow-pump/dossier.md` | `evals/cases/development/wp02-batch-01/author/snow-pump/dossier.md` | `dossier` |
| `sources/snow/snow-1855.txt` | `evals/cases/development/wp02-batch-01/sources/snow/snow-1855.txt` | `primary` |
| `sources/snow/parish-1855-b21363870.txt` | `evals/cases/development/wp02-batch-01/sources/snow/parish-1855-b21363870.txt` | `primary` |

Допустить все четыре файла через `editorial-source-import` с одним новым `--scope` (например, `snow-investigation-2026-11-06`), точным `--source-path` и указанным `--purpose`. В `source-map.json` сопоставить каждому `source_path` полученный RuntimeRef. Не смешивать новый бриф со ссылками другого scope из старого staging. Затем из корня репозитория, с обычными параметрами проекта, пользователя и хранилища:

```sh
uv run --project backend python -m podgen.writing_cli \
  --project "$PODGEN_PROJECT_ID" --actor "$PODGEN_ACTOR_ID" --objects "$PODGEN_OBJECTS_DIR" \
  editorial-import --input evals/editorial-inputs/current/snow-pump/shared.json \
  --source-map "$SNOW_WORK_DIR/source-map.json" --out "$SNOW_WORK_DIR/shared-ref.json"
```

После локальной проверки входа запускать с новыми `run-id` и каталогом экспорта. Профили должны разрешать соответствующие роли. Следующая команда вызывает модели:

```sh
uv run --project backend python -m podgen.writing_cli \
  --project "$PODGEN_PROJECT_ID" --actor "$PODGEN_ACTOR_ID" --objects "$PODGEN_OBJECTS_DIR" \
  run --run-id "$SNOW_RUN_ID" --case snow-pump \
  --author-ref "@$SNOW_WORK_DIR/shared-ref.json" --plan-mode own-plan \
  --planner-profile "@$SNOW_WORK_DIR/planner-profile-ref.json" \
  --plan-reviewer-profile "@$SNOW_WORK_DIR/plan-reviewer-profile-ref.json" \
  --writer-profile "@$SNOW_WORK_DIR/writer-profile-ref.json" \
  --reviewer-profile "@$SNOW_WORK_DIR/reviewer-profile-ref.json" \
  --export-dir "$SNOW_WORK_DIR/run"
```

Прежняя [пара v1](../../v1/snow-pump/shared.json), её [fixed-план](../../v1/snow-pump/fixed-plan.json) и документ-источник плана остаются историческим контролем. Старый fixed связан с точным digest старого shared и не применяется к новому. Для повторения прежнего прогона использовать сохранённые manifest, запросы, источники и версии промптов; текущая версия не переписывает архивы и frozen-инвентарь.

Пакет переведён в `/2`: card_id — устойчивый ключ, label и title — независимые отображаемые имена. Прежний точный JSON сохранён в `legacy-shared.json`; `card-id-migration.json` содержит соответствие и digests. Карточки можно переименовывать и переставлять без смены ID; документы и диапазоны цитат сохранены.
