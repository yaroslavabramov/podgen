# Импорт Snow от 10 ноября 2026

Из корня репозитория, с настроенным `PODGEN_DATABASE_URL` и доступом пользователя к проекту:

```sh
uv run --project backend python evals/editorial-inputs/current/snow-pump/import_case.py \
  --project "$PODGEN_PROJECT_ID" \
  --actor "$PODGEN_ACTOR_ID" \
  --objects "$PODGEN_OBJECTS_DIR" \
  --scope snow-materials-2026-11-10 \
  --out-dir "$SNOW_WORK_DIR"
```

Команда допускает четыре файла из таблицы в [README.md](README.md): неизменный бриф, **dossier-v5.md**, полный текст книги Сноу и полный текст приходского отчёта. Затем импортирует текущий **shared.json** с 24 карточками. Проверяются SHA и дословные диапазоны всех 68 выдержек; вызовов моделей нет. HTML, PDF и старые файлы не заменяются.

В `SNOW_WORK_DIR` сохраняются `D1-ref.json` … `D4-ref.json`, точный `source-map.json` и новый `shared-ref.json`. Для другого проекта или новой версии использовать отдельные scope и каталог результата.

Новый сквозной запуск выполняется отдельно: `--author-ref "@$SNOW_WORK_DIR/shared-ref.json" --case snow-pump --plan-mode own-plan`, с новыми `run-id` и каталогом экспорта. Фиксированного плана у этого пакета нет.

Для прежнего материала передать `--input evals/editorial-inputs/current/snow-pump/history/before-material-rebuild-2026-11-10.json`; он сам указывает на сохранённое `dossier-v4.md` и прежние три файла. Ещё более ранние снимки перечислены в README. Тот же импортёр читает перечень документов из выбранного пакета, не подставляя новое досье в старый.
