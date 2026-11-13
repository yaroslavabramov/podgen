# Hosted research: запуск и сохранение материалов

Реализация первого этапа плана `2026-11-13-research-engine.md`.

`podgen.research.hosted` хранит задание и выполняет один Gemini Deep Research job через Interactions API. `podgen.sources` получает версии документов по ссылкам отчёта. Материалы сохраняются в штатных Repository/ObjectStore с доступом проекта. Новая БД, очередь и собственный широкий исследователь не вводятся.

## Задание и профиль

`ResearchConfig` содержит `run_id`, полный `assignment`, английские `instructions` и профиль. `assignment.output_language` задаёт язык отчёта; `user_request` и `scope.source_mode=open_web` обязательны. Остальные поля задания сохраняются целиком. При наличии `episode_spec_ref` сохраняется и точный EpisodeSpec; режим supplied-only не допускает этот открытый веб-поиск.

Первое подготовленное задание находится в `evals/reports/2026-11-13-hosted-research/`. Тема — морская долгота и точные часы. Полный текст `request.txt` побайтно сравнен с фактическим `body.input`; это не сокращённый preview.

Профиль первого запуска: `deep-research-preview-04-2026`, REST `v1beta/interactions`, `Api-Revision: 2026-05-20`, `background=true`, `store=true`. Имя переменной с ключом фиксируется в `credential_env`; сам ключ в манифест и экспорт не попадает. Переключение провайдера или модели не производится.

## CLI

Общие аргументы:

```sh
python -m podgen.hosted_research_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --run-id RUN_UUID \
  --objects OBJECT_STORE --env-file .env \
  prepare --config research.json --export-dir EXPORT
```

Подключение к PostgreSQL берётся из `PODGEN_DATABASE_URL`. `prepare` сохраняет конфигурацию, запрос и hash без сети. Экспорт включает полный запрос, статус и манифест.

Операции с теми же общими аргументами:

```text
approve --request-hash HASH --reason REASON --accept-variable-provider-cost --export-dir EXPORT
resume --export-dir EXPORT
export --export-dir EXPORT
cancel --export-dir EXPORT
reprocess --export-dir EXPORT
acquire-sources --source-config sources.json --export-dir EXPORT
```

`approve` разрешает один submit конкретного запроса. Он не запускает исследование. Первый `resume` отправляет запрос, следующие читают известный job. Каждая команда выполняет одно обращение; процесс может быть остановлен и продолжен следующей командой. `cancel` отменяет известный job. `reprocess` заново разбирает сохранённый завершённый ответ без сети.

Это разрешение **одного задания с переменной стоимостью**, а не гарантированный денежный потолок. Указанные Google $1–3 для обычного Deep Research — оценка. Внутренние поиск и чтение определяет сервис. Нет автоматических повторных submit, автоматического включения billing или смены провайдера. [Документация и оценка стоимости](https://aistudio.google.com/docs/deep-research#availability-and-pricing).

## Восстановление

- До POST сохраняется intent. После ответа — полные bytes и HTTP metadata. Из ответа восстанавливается provider job ID.
- Если POST отправлен, но номер задания неизвестен, статус `outcome_unknown`: повторный `resume` не отправит второй POST. Нужна отдельная проверка исхода, а не автоматический новый запуск.
- Ошибка GET сохраняется и не стирает известный job. Следующий `resume` снова читает его.
- `failed`, `cancelled`, `incomplete`, `requires_action`, `requires_continuation` сохраняются как отдельные состояния. Неполный ответ не объявляется законченным исследованием; платное продолжение автоматически не создаётся.
- При завершённом job с неразобранным отчётом остаются raw response и `result_processing_required`; исправление парсера проверяется через `reprocess`.
- Экспорт связан с конкретным манифестом. Чужой непустой каталог не перезаписывается; файл привязки публикуется атомарно.

Usage сохраняется в нативном виде. `cost_usd=null` означает отсутствие подтверждённой стоимости, а не бесплатный запуск. Оценка не подменяет счёт провайдера.

## Полный отчёт и ссылки

Экспорт содержит `report.md`, `reported-sources.json`, полные ответы каждой операции и `result.json`. Все текстовые блоки `model_output` сохраняются в исходном порядке; служебные thoughts/tools остаются в raw response. Между блоками добавляется только разделитель. Mapping хранит путь к исходному блоку и диапазон в объединённом отчёте. Нативные byte offsets цитат не выдаются за индексы Unicode-символов.

Источники из annotations и ссылок в тексте имеют `reported_only`. Список не называется полным журналом чтения внешнего сервиса. Отчёт не пересказывается в факт-карточки и не становится доказательством каждого собственного утверждения.

## Получение источников

`sources.json` соответствует `SourceSelection`: собственный `operation_id`, extraction `profile_ref`, `rights` и необязательный список `urls`. Без `urls` выбираются все уникальные сообщённые ссылки; скрытого усечения нет. Явный список может содержать только URL из этого отчёта.

`profile_ref` — сохранённый `ProcessingProfile` с `profile_kind=extraction`, `implementation=saved-source-acquisition/1`, `version=1` и `AcquisitionProfile` в `configuration`. `rights` содержит `access_class`, ссылки на storage/publication policies. Их нельзя заменять выдуманными UUID. Jina и браузер включаются явно профилем; обычный профиль их не запускает.

Получение идёт последовательно. Завершённый URL сохраняется сразу; повтор команды продолжает тот же набор. Для каждой ссылки записаны исходные occurrences в отчёте и результат acquisition с `SourceRevision`/`SourceRepresentation` refs. HTTP original, MediaWiki API response, Jina и browser DOM не объявляются одними bytes. Поздний fetch не доказывает, какую версию страницы видел Gemini.

Текст представления извлекается из сохранённого оригинала. Точные цитатные связи остаются отдельным этапом; `no_exact_citation_binding` не означает, что ссылка проверена по содержанию. Служебные страницы, отсутствие документа и ошибки extraction имеют собственный статус.

## Проверка и следующий этап

До живого запуска проверены сценарии approval/hash, неизвестного submit, ошибки GET, восстановления по receipt, сохранения большого многоблочного отчёта, экспорта, источников и отсутствия ложных EvidenceSpan. Проверки транспорта используют подменённые ответы; это не подтверждает доступ реального аккаунта к Deep Research.

Первый живой результат принимается по `evals/reports/2026-11-13-hosted-research/review-criteria.md`. После его чтения подключаются material view и чтения планировщика/рецензента. Текущие планировщик, автор, фактчекер и их модели в этом изменении не меняются. Пригодность отчёта оценивается до этой интеграции; готовность всего пути «тема → выпуск» этими тестами не заявляется.
