# Вечерняя выдача на Вересковой

Положительный контроль редактора плана, `vereskovaya-evenings`. Библиотека, люди, числа и письма вымышлены. Текст плана написан специально для независимой проверки. Критерий оценки зафиксирован в `evaluation.md` и не входит в пакет.

- `shared.json` содержит пользовательский запрос, пять карточек и ссылки на текстовые оригиналы в этом каталоге. `sources/desk-log.txt` и `sources/reader-mail.txt` — полные документы со всеми датами и записями.
- `plan.json` — штатный снимок `PlanningOutputV9` для входа `candidate` на этапе `plan-review`. Идентификаторы карточек сохраняются в снимке, а при сборке запроса преобразуются в локальные ссылки редактора.
- Для оценки передать редактору `shared.json` через импорт и `plan.json` через `--candidate`. Оценку читать после ответа.

Из корня репозитория в окружении с рабочими `PODGEN_DATABASE_URL`, `PROJECT_ID`, `ACTOR_ID`, `OBJECTS_DIR` и отдельным каталогом `IMPORT_DIR`:

```bash
case_dir=evals/reports/2026-11-10-ending-comparison/controls/holdout
bash "$case_dir/import.sh"
```

Команда запускает `editorial-source-import` и `editorial-import` и сохраняет `$IMPORT_DIR/shared-ref.json`. Для штатного предпросмотра с профилем редактора плана:

```bash
PYTHONPATH=backend/src backend/.venv/bin/python -m podgen.writing_cli \
  --project "$PROJECT_ID" --actor "$ACTOR_ID" --objects "$OBJECTS_DIR" \
  preview --stage plan-review --run-id "$(uuidgen)" --case vereskovaya-evenings \
  --shared-ref "@$IMPORT_DIR/shared-ref.json" --candidate "$case_dir/plan.json" \
  --profile "@$REVIEWER_PROFILE_FILE" --out "$IMPORT_DIR/plan-review-wire.json"
```
