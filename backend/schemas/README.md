# Editorial input v1

Новый `writing_cli run` принимает `author_ref` на импортированный `editorial-input/1`.
Фиксированный режим дополнительно требует `--fixed-plan-input-ref`; собственный план запрещает эту ссылку.
Задание не ограничено двумя архивными темами: формат, язык, голоса и независимые target/min/max длительности задаются в JSON.

Канонические редакторские файлы: `evals/editorial-inputs/v1/<assignment.key>/shared.json` и отдельный `fixed-plan.json`.
Полные JSON, исходники, точные диапазоны цитат и привязки сохраняются без усечения. Цитаты проверяются по Unicode codepoints `[start,end)` со строгими границами. Общая часть никогда не ссылается на фиксированный план.
`shared_input_digest` — канонический `podgen.domain.canonical.digest` разобранного shared JSON.

`source-map.json` сопоставляет физические `source_path` с авторизованными RuntimeRef или SourceRepresentation ArtifactRef.
Для замороженных файлов проверяется происхождение импорта и единая область корпуса; копии фиксированных планов/контрольных текстов не допускаются как свидетельства.
Для новых заданий есть `editorial-source-import --file FILE --source-path PATH --scope SCOPE --purpose primary --out REF.json`;
назначения `brief`, `dossier`, `fixed_plan` также явные. Одна область `scope` связывает документы задания.

Все команды ниже имеют общий префикс:

```sh
uv run --directory backend python -m podgen.writing_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --objects /absolute/object-store
```

Импорт без модели:

```text
editorial-import --input shared.json --source-map source-map.json --out shared-ref.json
fixed-editorial-import --input fixed-plan.json --source-map source-map.json --shared-ref @shared-ref.json --out fixed-ref.json
```

Offline preview использует локальную БД (`PODGEN_DATABASE_URL`) и хранилище, без ключа API, клиента провайдера и endpoint подсчёта:

```text
preview --shared-ref @shared-ref.json --fixed-ref @fixed-ref.json --case ASSIGNMENT_KEY --stage draft --profile @writer-profile.json --run-id NEW_UUID --out preview.json
```

Для own draft: вместо `--fixed-ref` передать `--approved-plan plan.json` (чистый PlanningOutput).
Для `review`/`revise`: `--draft draft.json`, для правки также `--feedback review.json`.
Для `plan-review` нужен `--candidate`; для `plan-revise` также `--plan-review`.
Поддерживаются отдельные `anchor-repair` (`--draft`, `--feedback`) и `plan-target-repair` (`--candidate`, `--plan-review`).
Это чистые объекты контрактов, без архивных обёрток `output`.
Preview сохраняет полный wire и размеры instructions/input/schema; при превышении лимита файл остаётся доступен, команда возвращает validation error.

Новые границы: 128 КиБ суммарного JSON представления instructions/input/schema до создания клиента;
32 768 входных токенов на вызов после provider count, до submit.
Суммарные defaults: 196 608 входных и 131 072 выходных токена. Выход целиком резервируется по профилю, тексты не укорачиваются автоматически.
Исчерпание лимита останавливает запуск; сохранённые запросы и ответы доступны через `inspect`.

`keep` без обязательных правок и нерешённых вопросов завершает выпуск точным сохранённым draft, без revise invocation и оплаты повторной генерации.
`finalization` фиксирует источник и отзыв; экспорт содержит полный `final.txt` и совместимый `revise.txt`, явно помеченные `origin=unchanged_draft`, `model_call=false`.
`needs_material`, вопросы без свидетельств и противоречивые решения останавливаются после сохранения отзыва.

Исторические bytes, лимиты и схемы не пересобираются. Завершённые replay/inspect бесплатны.
Сетевое продолжение legacy отключено с `legacy_outbound_disabled_start_compact_run`; raw-флаг в CLI отсутствует.
Старый compiler доступен только тестам через явно внедрённый `legacy_offline_transport`, отдельно от обычной фабрики провайдера.

Локальная проверка реального корпуса и сохранённых сложных планов (без endpoint; старые адреса опор явно сопоставлены новым карточкам только в диагностической копии) дала следующие размеры полного видимого запроса:

| Роль | GPS, байты | Snow, байты |
|---|---:|---:|
| Собственный план | 33 285 | 46 171 |
| Проверка плана | 73 792 | 106 662 |
| Правка плана | 65 515 | 81 639 |
| Автор по собственному плану | 43 846 | 55 584 |
| Проверка текста | 67 295 | 105 019 |
| Правка текста, если нужна | 65 306 | 81 704 |

Это размеры запросов, а не измеренный расход нового модельного запуска. Реальный Snow anchor repair: три замечания, 29 предложенных точных диапазонов вместо 192, payload 14 572 байта. Ни один текст не усекался. Фактический token count применяется только при явно запущенном новом сетевом `run`.
