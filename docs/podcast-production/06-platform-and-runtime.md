# 06. Платформа и надёжное исполнение

Статус: спецификация целевой системы. Общие типы — [00](00-system-contract.md), предметные алгоритмы — 01–05. Здесь определены исполнение, хранение, восстановление, модельные вызовы и пользовательские операции. Это проектные решения, а не описание уже реализованного сервиса.

## 1. Стек и границы

Строим модульное приложение с несколькими типами workers. Предметное ядро не зависит от Temporal, HTTP, модели или базы данных. Temporal восстанавливает исполнение; редакционные агенты принимают содержательные решения. Надёжность исполнения не доказывает качество текста.

| Слой | Выбор | Ответственность |
|---|---|---|
| Domain/application | Python, Pydantic, отдельные use cases | Типы, политики, команды, контракты агентов |
| API | FastAPI, OpenAPI | Авторизация, команды, проекции, загрузки; длинный pipeline вне request handler |
| Исполнение | Temporal Python SDK | Workflow, timers, retries, recovery, cancellation |
| Canonical store | PostgreSQL, SQLAlchemy, Alembic | Артефакты, зависимости, права, transactional outbox, проекции |
| Retrieval | PostgreSQL FTS + pgvector, reranker adapter | Совместный поиск по источникам и заметкам |
| Оригиналы и медиа | S3-compatible object storage | Полные оригиналы, extraction, model payload, WAV, masters, exports |
| Клиент | TypeScript, React, OpenAPI-generated client | Создание, прогресс, playback, источники, revisions, export |
| Медиа | FFmpeg/ffprobe в изолированных workers | Нормализация, сборка, измерения по правилам 05 |
| Телеметрия | OpenTelemetry, метрики и логи | Сквозные task/invocation/artifact traces без raw текста в общих логах |

Это выбранный стек для реализации. Версии закрепляются в lockfiles/deployment manifest после CI совместимости; плавающие `latest` образы запрещены. Redis, Kafka, графовая БД и общий agent framework для этих операций не требуются. Отдельный search engine вводится при измеренном провале retrieval, а не по привычке.

Temporal требует детерминированной workflow-логики; API/DB calls размещаются в Activities. Поэтому LLM-вызов — Activity, сохранённый результат — вход следующего workflow-шага. Изменение исполняющихся workflows требует стратегии версий. [Temporal: versioning](https://docs.temporal.io/develop/python/workflows/versioning).

## 2. Модули и зависимости

```text
backend/src/podgen/
  domain/                 # общие типы, policies, pure invariants
  episodes/               # EpisodeSpec, branches, revision/release commands
  evidence/               # domain, acquisition, extraction, spans, retrieval, tools; 01
  research/               # editor, questions, notes, dossiers; 02
  writing/                # plans, scripts, listener state; 03
  verification/           # checks, reviews, release eligibility; 04
  audio/                  # performance, synthesis, assembly, alignment, QA; 05
  runtime/
    tasks/                # TaskSpec/TaskRun, inbox, adoption
    workflows/            # Temporal orchestration only
    context/              # manifests, aliases, compiler, read receipts
    models/               # invocation lifecycle, capability adapters
    tools/                # dispatcher, authorization, idempotency
  persistence/            # repositories, dependency index, blob adapters
  retrieval/              # query planner, sparse+dense search, reranker
  api/                    # REST, SSE, auth, projections
  operations/             # reconciliation, deletion, retention, deployment
frontend/src/features/    # episodes, editor, playback, sources, exports
contracts/                # generated JSON Schema/OpenAPI
evals/                    # cases, harness, evaluators, human-review import
```

Domain не импортирует SDK. Предметные пакеты вызывают ports: `ArtifactRepository`, `SourceReader`, `TaskDispatcher`, `ModelGateway`, `ObjectStore`, `ReleaseAssembler`; infrastructure implements ports. Researcher/writer не делает SQL и не запускает workflow инструментом модели. Общий runtime не определяет, что такое «хорошая кульминация».

Tools регистрации заметки, сценария и review принадлежат предметным пакетам. Dispatcher добавляет project/task/run IDs, разрешает alias и вызывает use case; модель не повторяет инфраструктурную обвязку. Один canonical type генерирует schema для API/tools; дополнительные предметные проверки остаются в owning module.

## 3. Canonical storage

Ссылки используют `ArtifactRef = {artifact_id, revision_id, kind}`. Artifact identity сохраняется между редакциями, revision ID никогда не переиспользуется. Envelope `ArtifactRevision` из 00 обязателен. Документ может храниться blob, но полный адресованный текст и checksum доступны через repository; внешний URL не заменяет snapshot.

Все пользовательские строки имеют `project_id`; `project.namespace_kind = tenant | catalog` по 00. Для содержательных artifacts связанные FK включают project_id и запрещают cross-tenant references. Единственное исключение — точные read-only refs опубликованных конфигураций catalog namespace через ConfigRefResolver, не произвольные cross-project links:

| Таблица | Ключ и содержимое |
|---|---|
| `projects`, `project_members` | Namespace kind, ACL, owner/editor/viewer, retention policy |
| `episodes`, `episode_runs` | Identity; run_id, purpose, generation_epoch, operational status, waiting/failure reason |
| `artifacts` | `(project_id, artifact_id)`, kind без изменяемого смыслового payload |
| `artifact_revisions` | `(project_id, revision_id)`, artifact_id, schema_version, payload, hash, producer, created_at |
| `artifact_dependencies`, `dependency_revalidations` | consumer revision → input revision, relation/selector; append-only revalidation ledger, active eligibility projection |
| `catalog_publications`, `catalog_references` | Published exact config refs, publisher/revocations, allowlisted audition assets; typed ссылки tenant consumer → catalog publication |
| `artifact_blobs` | revision → blob_id; purpose, byte length, media type, plaintext hash, encrypted locator |
| `branch_heads` | project/episode/branch, editorial_state_ref, lock_version, generation_epoch |
| `task_specs`, `task_runs`, `task_results` | Immutable spec; run/attempt/status; immutable result_id, run_id, TaskResult payload |
| `input_manifests`, `input_admissions` | Immutable manifest chain и admission decisions |
| `model_invocations`, `tool_executions`, `search_records`, `read_receipts` | Requests/responses, archived discovery/tool data, реально отправленный context |
| `acceptance_records`, `revocations`, `integration_events` | Append-only решения о точных версиях и adoption результатов |
| `commands`, `outbox`, `inbox` | API idempotency, delivery, deduplication |
| `source_chunks`, `embeddings` | Восстановимый индекс; source/representation/selector/profile |
| `release_manifests`, `export_jobs` | Индекс/проекция ref канонического ReleaseManifest ArtifactRevision, eligibility, exports; не вторая копия payload |
| `deletion_jobs`, `tombstones` | Отзыв доступа, удаление копий и состояние purge |

У роли приложения нет UPDATE/DELETE на `artifact_revisions`. Изменение содержания — новая revision; acceptance/revocation не меняет payload. Административное удаление — отдельная процедура §12. `UNIQUE(task_run_id)` на финальном результате; промежуточные notes сохраняются artifacts и progress events, а не перезаписью TaskResult.

ConfigRefResolver разрешает только published PolicyBundle/Policy/PromptDefinition/ToolsetDefinition/ModelProfile/ProcessingProfile/stock VoiceProfile/public PronunciationLexicon. Он проверяет kind/subtype, exact revision, catalog publication/revocation и consumer policy; FK catalog_references адресует отдельный опубликованный набор, а не обходит tenant FK общим nullable project_id. Источники, notes, scripts, media, private voices/lexicons и evaluation data не допускаются cross-tenant. Catalog publication — отдельное право оператора, не agent tool. Private custom profiles квалифицируются в своём tenant namespace.

Отдельное строго ограниченное исключение для медиа — `CatalogPublication.published_assets`00: опубликованные nonsensitive audition BlobRefs с точными hashes, media metadata и ACL. Только эти assets доступны директору/ассессору через safe projection, не произвольный AudioSegment/EvaluationResult квалификационного проекта. Публикация asset требует отдельного решения оператора; private voice samples/lexicon и secrets автоматически не раскрываются. Resolver проверяет publication eligibility при каждом чтении.

`content_hash = sha256:artifact-jcs-v1:<hex>` вычисляется из UTF-8 JCS-представления `{kind, schema_version, payload, input_refs, blob_hashes}` по RFC 8785. JCS фиксирует сериализацию чисел/literals и сортировку ключей; простого Python `sort_keys` недостаточно для совместимости с TypeScript. JSON numbers допустимы только finite IEEE-754, точные десятичные величины и большие integers кодируются типизированными decimal strings; невалидные Unicode/NaN/Infinity отклоняются. Null и отсутствующее поле различаются. Строки сохраняются без Unicode normalization. [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785).

`input_refs` — множество: dedup и сортировка по `(kind, artifact_id, revision_id)` до canonicalization; `blob_hashes` сортируются по `(purpose, hash)`. Предметные массивы payload, включая текстовый порядок, не сортируются. Hash исключает envelope.ref и envelope.created_at; IDs/timestamps внутри payload и input_refs сохраняются полностью согласно их семантике. Совпадение hash не переносит approval. Blob hash — по полным байтам. Дедупликация ограничена проектом. CI обязан сравнивать одинаковые JSON fixtures Python/TypeScript, включая decimal strings/null/Unicode; смена hash profile требует новой версии и явной миграции.

Сохранение blob: stream во временный объект → проверить размер/checksum → завершить immutable объект → DB-транзакция со ссылками и outbox. Сбой между object store и DB оставляет orphan, удаляемый reconciliation после safety window. Сбой после commit не создаёт второй результат: retry использует operation ID. Ссылка до завершения загрузки не публикуется. Контрактные тесты проверяют checksum/versioning/read-after-write именно выбранного S3-compatible поставщика.

Смысловые поля не режутся. API заранее сообщает inline limit; большие материалы идут streaming upload и blob ref. Превышение → `payload_too_large` с разрешённым маршрутом загрузки. Preview имеет отдельные provenance/размер и способ открыть полное; не подменяет body.

## 4. Manifests, admissions и видимый контекст

`TaskSpec.input_manifest_id` фиксирует исходное назначение. TaskRun хранит ordered chain manifests; каждый ModelInvocation — один точный manifest. InputManifest содержит `parent_manifest_id: UUID|null`, `revision_refs: ArtifactRef[]` и `runtime_refs: RuntimeRef[]` по 00. Продвижение branch/dossier не меняет работающий task неявно. `RuntimeRef = {record_kind, record_id}` адресует неизменяемую runtime-запись; для discovery здесь используются `search_record` и `tool_execution`. Ссылаемся на finalized ToolExecution output, не на изменяемую строку lifecycle попытки. Project/scope и existence проверяются столь же строго, как для ArtifactRef, но runtime-запись не притворяется evidence artifact.

```text
InputAdmissionRecord = {
  admission_id, project_id, task_run_id,
  parent_manifest_id, next_manifest_id,
  admitted_refs: ArtifactRef[], admitted_runtime_refs: RuntimeRef[],
  rejected_candidates: [{locator, reason_code}],
  origin: tool_result | editor_handoff | user_input,
  origin_id, policy_ref, scope_decision, created_at
}
ReadReceipt = {
  receipt_id, project_id, task_run_id, invocation_id, tool_call_id|null,
  manifest_id, artifact_ref, representation_ref|null,
  preview_ref|null, derived_from_ref|null,
  requested_selector, returned_selectors[],
  display_kind: preview | exact_text | rendered_page | audio,
  returned_content_hash, has_more, continuation|null
}
```

Admission = «разрешено использовать»; receipt = «artifact включён в конкретный отправленный payload». Ни один не доказывает понимание. `ReadReceipt` применяется только к чтению ArtifactRef: `read_source` создаёт candidate delivery, context compiler фиксирует receipt для invocation при отправке. При отмене до отправки хранится prepared payload `not_sent`, а не receipt доставки. Для transport outcome_unknown журнал различает отправленное приложением и подтверждённое provider получение.

Discovery snippets сохраняются в append-only SearchRecord01 и ToolExecution с provider response/query/временем и checksum; затем admitted как runtime_refs. Их включение в exact request видно через ContextAssembly и invocation archive. Из поискового сниппета не создаётся фиктивный EvidenceSpan/DerivedPreview и ему не выдаётся ReadReceipt первоисточника. Ни search rank, ни metadata не подтверждают содержание ещё не полученного документа.

Discovery: search metadata → acquisition сохраняет SourceRevision/Representation → policy проверяет ACL/source mode/domain restrictions → атомарно admission + следующий manifest → alias registry дополняется → next call получает результат. Запрещённый источник не попадает в corpus/context. Новое издание — новый ref и alias; `S12` в пределах task никогда не переключается на другое содержимое.

Compiler сначала включает цель/смысл задания, затем редакционный контекст и релевантные полные материалы, затем ссылки на открываемый остаток. Необходимый текст не вытесняется историей tool calls. Недостаток окна → `context_capacity_exceeded`; use case делит задачу, меняет разрешённый profile либо создаёт отдельный summary artifact с доступом к полному оригиналу. Молчаливое усечение истории запрещено.

`ContextAssembly = {manifest_id, ordered_blocks, included_refs, included_runtime_refs, excluded_refs_with_reasons, token_estimate, tokenizer_version, request_blob_ref, request_hash}`. Содержательный блок — typed union `{kind:artifact, artifact_ref, selector, display_kind, trust_level, content_hash}` либо `{kind:runtime, runtime_ref, selector, trust_level, content_hash}`. Instructions/task blocks отдельно адресуют закреплённые prompt/TaskSpec; runtime block не может заявить display_kind=exact_source. Блоки сохраняют порядок и source labels. Внешние «system prompt», XML-инструкции и tool calls остаются данными.

Для извлечения ListenerState компилятор берёт `spoken_view` точного сценария или prefix по 03. У первого независимого чтения из 04 отдельные invocation, нейтральное задание, versioned toolset и явная политика представления. Разрешены произносимые реплики в порядке воспроизведения, точные ScriptSelector и слушательские предпосылки, разрешённые 04. В ContextAssembly сохраняются view name/version, порядок выбранных диапазонов и хеш фактически отправленного текста.

Dispatcher применяет ограничения ко всем путям чтения. Запрос полного ScriptRevision, редакционных ремарок, bindings, плана, досье, чужой оценки или произвольный retrieval выходит за scope первого чтения. Технические сохранения результата и постраничное чтение остальных произносимых реплик разрешены. История авторского вызова, cached result другой роли и скрытые hints из общего TaskSpec не входят в запрос. В следующем проходе reviewer получает материалы по правилам 04 после сохранения независимого ответа. Полный исходный сценарий и его прежние версии доступны автору через его собственный role scope.

В режиме чтения по префиксам диспетчер сначала ограничивает все чтения текущей границей `spoken_view`. Следующее окно открывается новым manifest после финализации `record_first_read(mode=prefix)`. Для произносимых реплик `ReadReceipt.display_kind=exact_text` относится к конкретным диапазонам этих реплик. Идентификатор представления и граница окна закреплены в ContextAssembly. Итоговый ToolExecution `record_first_read(mode=final)` связывает наблюдения и открывает фазу сравнения с замыслом по 04.

Пример: researcher начал с `M0={Dossier@7,S1@2}`. Acquisition добавил `S2@1`, admission A1 создаёт `M1(parent=M0)`. I4 закрепляет M1 и видит preview S2; I5 после read_source видит страницы 4–6. Receipt I4 не подтверждает полный S2. Здесь `S2@1` — условное сокращение точного ArtifactRef, не реальный UUID.

## 5. Tasks и результаты

TaskSpec — неизменное задание; TaskRun — одна попытка его исполнения. Retry HTTP не создаёт новый TaskRun. Новая цель/исходный corpus/acceptance questions → новый TaskSpec с `supersedes_task_id` в operational metadata.

Семантический `TaskResult.status`: `answered | partial | needs_material | unanswerable | failed`. Операционный `TaskRun.status`: `queued | running | waiting | cancel_requested | cancelled | succeeded | failed`. `succeeded + partial` означает надёжно сохранённое неполное исследование; это никогда не условие качества выпуска.

| Событие | TaskRun | TaskResult |
|---|---|---|
| Получен ответ | succeeded | answered + output refs |
| Найдена часть, достигнут лимит | succeeded | partial + notes + remaining questions |
| Нужен отсутствующий материал | succeeded | needs_material; editor определяет продолжение |
| Обоснована неизвестность | succeeded | unanswerable + источники и границы вывода |
| Сеть не восстановилась | failed | failed + failure taxonomy; outputs сохранены |
| Пользователь отменил | cancelled | Финальный TaskResult необязателен, checkpoints остаются |

TaskRun содержит run_id, task_id, attempt, episode_run_id, generation_epoch, current_manifest_id, last_checkpoint_ref, timestamps, limits, failure. Deadline/max calls ограничивают цикл, не достаточность исследования. Перед лимитом runtime просит сохранить partial; если модель не ответила, backend завершает failed с checkpoints, не сочиняя содержательный вывод.

`EpisodeRun.status` имеет отдельный operational enum: `queued | running | waiting | needs_attention | cancel_requested | cancelled | succeeded | failed`. `EditorialState.phase` описывает предметный этап. `ready/current_ready` — проекция eligibility конкретного ReleaseManifest, а не значение operational enum. `EpisodeRun.succeeded` означает завершение orchestration с сохранённым результатом; API отдельно сообщает, есть ли готовый выпуск. `waiting` — ожидается автоматическое событие, `needs_attention` — продолжение требует явно указанного решения/восстановления, а не скрытого фонового цикла.

`EpisodeRun.purpose = production | evaluation` задаёт авторизованный scheduler, TaskRun наследует purpose неизменно. Qualification/capability probes исполняются как evaluation с pinned EvaluationCase и allowlist candidate profiles. Evaluation namespace отделён ACL от пользовательского production; candidate не требует прежней qualification, но не обходит source/privacy/tool policy. Ни модель, ни пользовательский tool arg не могут сменить purpose. Evaluation outputs сохраняются для оценки, но этот run не создаёт пользовательский ready/export; production требует действующую qualification exact profiles в своём scope.

TaskResult хранится как `{result_id, run_id, payload: TaskResult}`; result_id — operational UUID, не ArtifactRef. IntegrationEvent связывает result_id, target EditorialState revision, adopted/rejected output refs и решение. Неинтегрированные результаты — inbox projection, не растущий массив в EditorialState.

## 6. Orchestration и редакционный commit

`EpisodeWorkflow(episode_run_id, generation_epoch, policy_ref)` управляет research/writing/verification/audio child workflows. History содержит IDs, control events, небольшие результаты и timers; HTML, scripts, prompts/audio остаются в application storage. Для долгих runs — Continue-As-New с control cursor и открытыми task IDs после завершения либо явной передачи незаконченных interactions. [Temporal: Continue-As-New](https://docs.temporal.io/develop/python/workflows/continue-as-new).

Один устойчивый model step: подготовить invocation → submit/reconcile → сохранить response → исполнить разрешённые tools → обновить manifest → следующий шаг. Не помещать бесконтрольный долгий agent loop в одну Activity. Общие helpers допустимы; семантика research/audio остаётся предметной.

Workers параллельно сохраняют ответы. Только editor интегрирует head:

```text
1. Прочитать EditorialState@E7 и inbox результата R12.
2. Вне DB transaction подготовить decision и содержательные artifacts.
3. Commit(expected_head=E7, expected_epoch=4, result_ids=[R12]):
   проверить input eligibility, ACL/policy и отсутствие adoption R12;
   записать DossierSnapshot@D8, EditorialDecision@J8, EditorialState@E8;
   CAS branch_heads E7 -> E8 + IntegrationEvent + outbox; одна transaction.
4. CAS conflict: proposal остаётся неинтегрированным; перечитать head;
   редактор решает merge/rebase/research, runtime не сливает прозу сам.
```

Lease снижает конкуренцию; корректность дают CAS и `generation_epoch` fencing token. Cancel/новое намерение повышают epoch. Старый worker может дописать diagnostic result, но не двигать head/выпускать release. Повтор commit возвращает прежний результат по operation_id. Late result сохраняется: editor явно использует его или объясняет неиспользование.

EditorialState.phase задаёт редакционный этап; EpisodeRun.status показывает operational состояние из §5. Один projector последовательно обрабатывает episode sequence и соединяет их для UI, не смешивая enum. Новый run пользовательской revision не отменяет воспроизводимость старого ready bundle; `current_ready` отдельно указывает допущенный bundle для текущих intent/head/policy.

При интеграции production-сценария editor service проверяет согласованность EpisodeSpec, DossierSnapshot и StoryPlan по равенствам 00. Текстовые отчёты создаются следующим этапом для уже закреплённой версии. Принятие D2 делает прежнюю пару (P1,D1) непригодной для production-письма под D2. Предшествующие артефакты остаются доступными для сравнения. Editor принимает P2 через EditorialDecision и IntegrationEvent с проверкой ожидаемых head/epoch. Сохранение S2 под P1 вместе с D2 возвращает `stale_revision` со ссылками на использованные входы. Переходы обратно в planning берутся из автомата 00.

Проекция открытых ResearchRequest строится по регистрации и принятым request_dispositions из 02. Запись регистрации связывает exact request, requester, рабочую ветку, epoch и sequence. Запрос непринятого кандидата отслеживается на его ветке и учитывается при принятии кандидата либо его следующей версии. Работник завершённого или отменённого epoch не меняет активную ветку. Запрос в действующем epoch на прежний текст этой ветки остаётся открытым до принятого переноса, ответа или решения об устаревании основания. Операция с тем же idempotency key повторно возвращает ту же запись.

Регистрация действующего passage/central_answer и принятие его disposition изменяют branch validity_version в одной транзакции с событием. Проектор применяет решения по sequence к точному subject. Пустые списки автора и отсутствие placeholder не закрывают зарегистрированный запрос. Если версия, смысл фрагмента или опора принятого решения изменились, editor сверяет применимость закрытия к новой версии по 02. `blocking=none` учитывается для планирования и допускает выпуск без результата.

Один runtime preflight проверяет отсутствие блокирующих запросов текущей ветки при текстовом допуске, перед TTS и при сборке. Для допуска используются согласованная проекция и её sequence/validity version. При отставании проекции сервис дочитывает журнал до текущего watermark либо откладывает действие. Если R зарегистрирован после текстового допуска или во время рендера, очередная проверка видит новую версию. Результат нельзя принять на устаревшей версии допуска. Финальный commit и регистрация блокирующего R сериализуются на ветке. Принятый фактический ответ, объявленная в тексте неопределённость и обоснованное исключение фрагмента оценивает редактор по конкретному запросу.

Для выставления пользовательского ready release assembler одной транзакцией проверяет exact refs ReleaseManifest из 00, включая обязательный `show_notes_review_ref` из 04, соответствующие acceptance/revocation, active dependency eligibility, production purpose, scope и epoch; сохраняет канонический ArtifactRevision(kind=ReleaseManifest), eligibility record и проекцию. Таблица release_manifests содержит ref/index этого artifact, а не независимую копию bundle. Завершённый EpisodeRun получает succeeded; current_ready вычисляется отдельно. Изменение сценария во время сборки → CAS conflict и пересборка. Запрет несовместимого bundle — invariant, проверяемый гонкой script edit/release assemble.

ReleaseManifest имеет `purpose:production|evaluation` и `evaluation_case_ref:ArtifactRef|null` из 00. Evaluation использует тот же assembler и проверки полноты/согласованности/качества; единственное исключение — отсутствие production qualification у candidate profile, явно разрешённого pinned EvaluationCase. При успешных проверках создаётся internal evaluation manifest с обязательным case ref. Он никогда не участвует в пользовательском current_ready/export, даже если содержательные checks прошли; evaluator-only playback разрешён по ACL. Если содержательные проверки не прошли, сохраняются failed EvaluationResult и outputs, а успешный manifest не выдаётся. Это позволяет проверить полный pipeline до квалификации без обхода production допуска.

## 7. ToolResult, идемпотентность и retries

Общий model-facing ответ инструмента — discriminated union:

```text
ToolResult<T> =
  {ok:true, data:T, warnings:Warning[], continuation:null|{cursor:string}}
  | {ok:false, error:ToolError}
ToolError = {
  code:string, message:string, retryable:boolean,
  recoverable_actions:[{action, explanation}], details_ref:null|ArtifactRef
}
Warning = {code, message, affected_refs:ArtifactRef[]}
```

`code` — lower_snake_case. `data` и `error` не возвращаются одновременно. `recoverable_actions` перечисляет разрешённые tools/действия, не произвольные shell-команды. Длинные diagnostics доступны по защищённому details_ref. Идентификаторы исполнения/request/manifest — trace metadata, не поля, которые модель повторяет в args.

Общие ошибки: `outside_scope`, `access_denied`, `unsafe_destination`, `unavailable`, `unsupported_media`, `extraction_incomplete`, `ambiguous_selection`, `revision_unavailable`, `field_too_long`, `temporarily_unavailable`, `context_capacity_exceeded`, `payload_too_large`, `stale_revision`, `cancelled`. Точные предметные условия определяют 01–05. `retryable` означает полезность повторения **той же** операции; recoverable actions могут предлагать изменение запроса даже при retryable=false.

Temporal retries могут повторить Activity; идемпотентность обеспечивает приложение. [Temporal: error handling](https://docs.temporal.io/develop/python/best-practices/error-handling). `operation_id` выводится из logical command + task/run + step + input fingerprint; номер сетевой попытки в ключ не входит. API key связывается с hash тела/actor/project: другое тело → `idempotency_key_reused`, то же → прежний результат. Tool execution имеет UNIQUE `(invocation_id, provider_tool_call_id)` и args fingerprint.

DB mutation и outbox-event сохраняются вместе. Dispatcher доставляет как минимум один раз, consumer дедуплицирует event_id. Reconciliation сверяет committed commands, Temporal runs и deliveries. Вызов внешнего API и DB commit не объявляются атомарными.

| Ошибка | Действие |
|---|---|
| Timeout/429/временная 5xx | Retry с jitter, deadline, Retry-After и общим provider rate limiter |
| Schema/profile не поддержаны, credentials неверны | Configuration failure без бессмысленных повторов; diagnostic, run ждёт восстановления |
| Context/output capacity | Изменить размер задачи/profile отдельным решением, не повторять одинаковый запрос |
| Model refusal | Сохранить отказ; не маскировать пустым JSON и не переключать провайдера ради обхода |
| Неверные tool args | Typed error с путём поля/примером, ограниченная попытка исправления |
| Ответ submit потерян | `outcome_unknown`; retrieve по известному provider ID, затем reconciliation/replacement |
| Частичный stream | Raw diagnostic сохраняется; нет валидного сценария/TaskResult без завершения и validation |
| Два completed provider результата | Оба сохранены, только выбранный invocation adopted через CAS |

До submit сохраняется request intent; полученный provider ID записывается сразу. Idempotency/retrieve/cancel используются лишь при проверенной capability, универсального exactly-once нет. Для чистого вычисления после reconciliation window допускается новая invocation с `replaces_invocation_id`; поздний ответ не принимается автоматически. Мутации наших tools остаются идемпотентными при любом числе model requests.

Стартовая transport retry policy: transient errors — до 5 попыток в пределах deadline, backoff 1–30 с с jitter; schema/auth — 0 повторов. Это эксплуатационная настройка для калибровки. Семантические revisits/rescue регулирует 02. Retry не переписывает неверную заметку и не превращает partial в answered.

## 8. ModelGateway и журнал вызова

Адаптер провайдера реализует `capabilities`, `submit`, `retrieve_if_supported`, `cancel_if_supported`, `normalize_events`. Он не считает reasoning/tool-choice параметры разных API взаимозаменяемыми. Общие события: text chunk, tool request, usage, completed, incomplete, refused, provider error; оригинальный ответ сохраняется.

```text
ModelProfile.payload = {
  provider, api_family, model_id, model_revision_or_alias,
  adapter_version,
  allowed_roles[], supported_modalities[], context_limit, output_limit,
  structured_output_mode, tool_call_mode, parallel_tool_policy,
  generation_parameters, timeout_policy,
  data_retention_policy_ref, caching_policy, fallback_profile_refs[],
  configured_languages[]
}
CapabilityReport.payload = {
  profile_ref, tested_configuration_fingerprint, adapter_version,
  checks: [{capability, expected, observed, outcome, diagnostic_refs[]}],
  observed_limits, probe_run_ids:UUID[], probe_invocation_ids:UUID[], limitations[]
}
ModelInvocation = {
  invocation_id, task_run_id, step_id, manifest_id,
  profile_ref, prompt_ref, toolset_ref,
  context_assembly_ref: RuntimeRef|null,
  request_blob_ref, request_hash, provider_request_id|null,
  response_blob_ref|null, usage, status, failure,
  replaces_invocation_id|null, started_at, completed_at|null
}
```

ContextAssembly — runtime record, не ArtifactRevision: непустой context_assembly_ref имеет record_kind=context_assembly и адресует immutable сборку. Для language-model invocation с compiled context ссылка обязательна. Null разрешён для прямого media/provider вызова без context compiler; полный фактический request_blob_ref всё равно обязателен. Суффикс `_ref` сам по себе не делает поле ArtifactRef: тип указан явно.

ModelProfile и VoiceProfile05 — чистые immutable configurations. `configured_languages` и limits описывают запрос конфигурации, а не доказанную поддержку; CapabilityReport проверяет exact profile/fingerprint. Report ссылается на существующий profile, EvaluationResult04 — на candidate profile; обратных refs на будущие capability/evaluation/audition результаты в profile нет. Так qualification не создаёт DAG-cycle и не требует создания нового профиля после каждого теста.

Production eligibility определяется отдельным AcceptanceRecord00: `check_kind=profile_qualification`, `subject_ref=exact profile`, `reviewed_input_refs=[CapabilityReport,EvaluationResult,...]`, policy и `assessment_scope:QualificationScope` по 00. Область содержит явно квалифицированные supported_cells; независимые списки языков/форматов/длительностей не образуют неявное декартово произведение разрешений. Это не новый QualificationRecord artifact. Service-side проекция выбирает действующие accepted records с учётом revocations и точной комбинации. Raw evaluation/annotations доступны только уполномоченному evaluator/operator, не generation model через обход refs. Безопасные явно published audition assets доступны директору/ассессору по §3; это не доступ к сырым eval outputs. Модель получает safe qualification projection, её tool output архивируется ToolExecution. Exact catalog profile/acceptance не копируются по tenants. Capability pass не заменяет продуктовую qualification. Начальные candidate probes разрешены только по evaluation purpose §5, не дают userready.

Если provider не предоставляет immutable model revision, profile честно хранит alias. Локальный profile version не обещает идентичные веса: drift отслеживает canary evaluation; trace сохраняет metadata ответа. Fallback требует qualification роли/языка/длительности/формата и сочетания голосов заранее, переключение фиксируется явно. Отрицательный drift сигнал отзывает eligibility, не изменяя profile/history.

Structured Outputs используются для формы результата, function tools — для действий, когда capability поддержана. Schema не доказывает истинность. Refusal и incomplete обрабатываются до разбора типизированного содержимого. [OpenAI: Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

Журнал сохраняет полный app-controlled request, tool schemas, выбранные параметры, ordered messages, tool outputs и API response. Внутренние provider system messages/hidden reasoning не объявляются наблюдаемыми; воспроизводимость относится к отправленному приложением и полученному через API. Скрытый chain-of-thought не запрашивается. EditorialDecision содержит краткое проверяемое основание со ссылками.

Foreground — базовый режим; background включается только после проверки capability, retention policy и retrieve/reconciliation. Provider job не заменяет durable workflow. [OpenAI: background mode](https://developers.openai.com/api/docs/guides/background). Если privacy policy запрещает persistence провайдера, adapter должен выбрать совместимый режим либо вернуть явную несовместимость, а не изменить policy молча.

Prompt caching оптимизирует неизменный префикс, не повторяет прежний ответ. Порядок instructions/tools стабилен, динамический context — отдельными блоками. [OpenAI: prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching). Application result cache отдельно включает project/scope, все input refs, policy/prompt/toolset/profile revisions, parameters/compiler version. Revoked dependencies исключают reuse. Никаких cached artifacts между private projects.

## 9. Retrieval и индексы

Chunks адресуют SourceRepresentation стабильными selectors. Индекс — восстановимая проекция, не evidence. Embedding revision закрепляет model/dimension/preprocessing/source. Смена extraction/model создаёт новый index namespace; переключение после recall comparison, смешивать vector spaces нельзя.

Query planner объединяет lexical, multilingual dense и exact entity/date/identifier ветви: RRF для кандидатов, reranker для релевантности вопросу. Его score не равен truth/confidence. pgvector предусматривает FTS + RRF/cross-encoder, но не гарантирует качество поиска. [pgvector: hybrid search](https://github.com/pgvector/pgvector#hybrid-search).

FTS config выбирается по языку фрагмента; русский/английский — соответствующие configs, неизвестный — simple плюс dense/exact branch. Имена/числа/идентификаторы имеют отдельные нормализованные поля; оригинал сохраняется. Dictionaries выполняют language-dependent normalization, поэтому одинаковой морфологии всех языков не обещаем. [PostgreSQL: dictionaries](https://www.postgresql.org/docs/current/textsearch-dictionaries.html).

ACL, source_scope, разрешённые revisions/deletion status фильтруются внутри retrieval до выдачи. Catalog configuration resolver отделён от содержательного поиска: доступ к profile не открывает corpus/evaluation data каталога. ANN под фильтром может терять candidates: iterative scans, fallback exact search ограниченного корпуса, измерение против exhaustive ground truth. При необходимости tenant partitioning/отдельный индекс. [pgvector: filtering](https://github.com/pgvector/pgvector#filtering).

FTS ranking не называется BM25. Если recall на multilingual names/dates/tables не проходит cases 01/04, меняется retrieval adapter на специализированный sparse engine; evidence contract не меняется. Trigger — измеренная ошибка, а не произвольный размер БД.

## 10. API и пользовательский путь

Mutation требует actor/project authorization и Idempotency-Key; изменение ветки — expected revision/If-Match. Ответ команды: `{command_id, episode_run_id?, status, links}`; длинная операция → 202. Ошибка API: `{code,message,retryable,details,trace_id}` без prompts/secrets/существования чужого ID.

| Endpoint | Поведение |
|---|---|
| `POST /projects/{id}/episodes` | Создать EpisodeSpec и run |
| `POST /projects/{id}/uploads` | Upload session; complete проверяет bytes/checksum и запускает acquisition |
| `GET /episodes/{id}` | Этап, outputs, waiting/error и permitted next actions |
| `GET /episodes/{id}/events?after={sequence}` | SSE, возобновление по sequence; без hidden reasoning |
| `GET /episodes/{id}/artifacts/{artifact_id}/revisions/{revision_id}` | Полный доступный объект/provenance; blobs через scoped short-lived URL |
| `POST /episodes/{id}/cancel` | Fenced cancellation текущего run |
| `POST /episodes/{id}/resume` | Новый EpisodeRun от checkpoint/selected revision |
| `POST /episodes/{id}/revisions` | Новое намерение/текст; новая revision/ветка и explicit invalidation |
| `POST /episodes/{id}/exports` | MP3/WAV, transcript, show notes, manifest точного ready release |
| `DELETE /projects/{id}/sources/{artifact_id}` | Tombstone + purge workflow с dependency impact |

EpisodeSpec имеет один canonical schema в 00. API принимает тему, аудиторию, `language`, `duration={target_seconds,tolerance_seconds,mode:target|strict}`, host profiles/format, `temporal` и source policy. Defaults: target=1800 с, tolerance=120 с, mode=target; язык из явного запроса/сохранённой настройки пользователя; если настройки нет — язык текущего запроса. Format=solo с одним квалифицированным stock voice profile выбранного языка. Defaults показаны перед запуском и записаны в spec.

В режиме target editor может предложить содержательно оправданную длительность за допуском; это не разрешение незаметно нарушить spec. Strict не исполняется filler/ускорением голоса: если качественный материал не укладывается, `needs_attention` с вариантами изменить длительность/scope.

Единственный временной контракт: `EpisodeSpec.temporal={mode:current|historical_as_known|retrospective, requested_as_of:timestamp|null, knowledge_as_of:timestamp, subject_period:period|null}`. Top-level as_of/requested_as_of не вводятся. Backend закрепляет knowledge_as_of при создании spec: для current/retrospective — запрошенный cutoff либо время создания UTC, для historical_as_known — явно указанный исторический cutoff. Current использует знания на cutoff; historical_as_known допускает только известное тогда; retrospective рассказывает о subject_period с более поздним знанием до knowledge_as_of. После resume cutoff не двигается молча. Research/freshness/factcheck/show notes получают тот же temporal object через pinned EpisodeSpec; источник оценивается по допустимому времени знания, а не только дате скачивания.

Выбор языка, длительности, формата и voice допустим только по support matrix, квалифицированной 04/05. Нельзя обещать все 90+ минут/языки одним флагом. Неподдержанный запрос возвращает конкретную несовместимость и допустимые комбинации; он не деградирует молча в другой язык/одного ведущего.

Правка EpisodeSpec создаёт revision и новый EpisodeRun, повышает epoch прежней текущей ветки. Старый ready bundle доступен с прежними refs и пометкой версии. `current_ready` вычисляется для нового намерения отдельно; пока новый выпуск не проверен, старый не выдается за обновлённый. Resume отменённого run создаёт новый run, а не меняет cancelled обратно на running.

UI прогресса показывает проверяемые этапы/причины: «дополняем источник для числа», «проверяем текст», «перезаписываем две реплики», последнее событие и preview. Процент от числа calls не считается готовностью. Estimate длительности не выдаётся за измеренную длительность audio. На результате — playback, синхронный transcript, источники/оговорки, версия, export, revision; raw research доступен дополнительно, не обязательным ритуалом.

Локальная аудиоправка из 05 §16 проходит через один application service: пока без UI — CLI, затем тот же контракт API. Timestamp запрашивает proposal, а не TTS. Ответ содержит exact parent/version/hash, одно-два source sentences, frames/rate, voice/settings identity и audio preview с соседями. Approval отдельной командой закрепляет конкретный proposal hash. Revised proposal требует нового approval; submit и adoption проверяют тот же явный parent ref/hash и его bytes. Ветка от старого immutable master допустима; локальный инструмент не требует глобального latest pointer. ASR/LLM/TonePerfect verdict не является admission condition. Результат — новая версия, сдвинутая timeline и оба join previews; `assembled` не выдаётся за уже прослушанное качество.

Отмена — не rollback сохранённого. Workflow отменяет pending tasks/provider jobs где поддержано, останавливает media subprocess, сохраняет checkpoints. Длинные Activities heartbeat + heartbeat timeout нужны для доставки отмены; grace period не отменяет fencing поздних результатов. [Temporal: cancellation](https://docs.temporal.io/develop/python/workflows/cancellation). UI держит cancel_requested до подтверждения. Внешняя публикация/рассылка не следует автоматически из ready/export; будущая публикация — отдельная явно вызванная команда конкретного release.

## 11. Invalidation и reuse

`ArtifactRevision.input_refs` и полный invocation trace фиксируют происхождение. Это не список вечных активных quality dependencies. В dependency index relation: `lineage | uses_content | uses_order | applies_policy | renders | verifies`. Parent/history/previous-review refs — lineage: сохраняют историю, но не переносят ошибку предка автоматически на исправленную revision. Остальные отношения участвуют в текущей eligibility closure. Доступность в manifest сама по себе не доказывает содержательную зависимость; producer указывает использованные inputs, backend проверяет admission. Пока точность выбора не проверена, реально показанные содержательные входы считаются uses_content консервативно.

Автор может предложить retained/replaced/removed зависимости, но не вправе сам снять invalidation. Application service применяет оценку полномочного владельца exact текущей revision через общий runtime event:

```text
DependencyRevalidationEvent = {
  event_id, subject_ref:ArtifactRef, assessment_ref:ArtifactRef,
  authority:{kind:acceptance_record|integration_event, record_id:UUID},
  dispositions:DependencyDisposition[],
  expected_validity_version, policy_ref, created_at
}
```

DependencyDisposition и subject_selectors используют канонический контракт00; selectors типизированы owning schema registry, а не универсальными script offsets. Normalizer проверяет kind, точную revision и соответствие owner assessment: например, каждый `VerificationReport.dependency_dispositions[i].checked_against_script_ref`04 должен совпасть с `event.subject_ref` и `VerificationReport.script_ref`. Runtime не переименовывает script-specific поля чужого report и не принимает disposition от произвольной модели.

| Subject | Assessment и полномочие |
|---|---|
| ScriptRevision | VerificationReport04 + factual AcceptanceRecord exact script |
| ShowNotes | ShowNotesReview04 + show_notes AcceptanceRecord exact notes |
| AudioSegment/AudioMaster | AudioQAReport05 + audio AcceptanceRecord exact subject/scope |
| ResearchNote, StoryPlan | EditorialDecision02 с reviewed_output_refs/dispositions + IntegrationEvent, принявший exact outputs. Для оценки всего StoryPlan допустим WholeArtifactSelector |
| Code-built DossierSnapshot/manifest | Active edges вычисляются детерминированно из current payload refs; previous snapshot — lineage |

Для model-authored prose консервативная связь действует до owner review: removed/lineage_only требуют проверки отсутствия зависимого утверждения во всём subject, replaced — новой опоры. Для code-built контейнера удаление прежней ссылки из manifest не требует отдельного модельного disposition; полный текст его дочерних notes всё ещё подчиняется их собственному review. Если контейнер содержит сгенерированное смысловое резюме, эту часть проверяет владелец prose, а не детерминированный manifest builder.

Event append-only; projector пересчитывает active edges с CAS по expected_validity_version. Authority обязан быть действующим и точно принимать subject/assessment. Принятие assessment, запись event и обновление eligibility согласуются транзакционно: кандидат не проходит без проверки и не остаётся навсегда заблокирован прежним основанием. Повтор события идемпотентен; потеря validity CAS требует перечитать новые revocations/оценки, не перезаписать их.

Старые input_refs/trace/edges не удаляются. Новая revalidation меняет только eligibility subject_ref; она не снимает зависимость другого dossier, ShowNotes или script revision. Retained сохраняет edge; replaced связывает новые exact refs; removed/lineage_only убирают прежний edge из **quality** closure. Privacy/access revocation и purge используют полную историю копирования и не отменяются редакционным disposition.

Новая source revision создаёт supersession event. История воспроизводима; eligibility projector помечает затронутые ветки needs_revalidation по policy и EpisodeSpec.temporal. Старое издание может подходить historical_as_known; исправление ошибки/retraction всё равно требует содержательной оценки. Citation не переписывается задним числом. Алгоритм: reverse **active quality** closure → impact event → отзыв affected eligibility → targeted tasks → reports/revalidation → новый ReleaseManifest. Удаление/утечка данных использует отдельную полную provenance closure §12.

Render key05 сравнивает фактические spoken content, content-only normalization mapping, voice/model/profile, lexicon/control tags/settings, реально переданный соседний context/audio identity и границы units. Он исключает script_ref/plan_ref/случайные IDs: новая текстовая revision при неизменном исполнении может повторно использовать bytes. Exact script selectors/PerformancePlan refs сохраняются отдельной provenance, а `reuse_basis_ref` — lineage к прежнему сегменту; действующие render inputs и privacy restrictions остаются проверяемыми.

Reuse создаёт новую AudioSegment revision с теми же проверенными blobs, новым exact script/plan binding и основанием. Прежний QA не переименовывается: новые refs требуют нового допуска, montage/transcript/master QA по 05, даже если часть measurements повторно использована. Если изменена только опора текста, пересинтез может не требоваться, но verification/acceptance/manifest пересобираются. ListenerState не наследуется по совпавшему act title.

## 12. Доступ, внешние материалы, удаление

API проверяет ACL; worker получает service identity + task-bound project/scope capability. В БД RLS, application role не владеет таблицами и не имеет BYPASSRLS; migrations — отдельная роль. Published config catalog читается через отдельный policy-constrained resolver §3, а не broad RLS bypass; raw qualification/evaluation traversal generation worker запрещён. RLS дополняет application/object-store checks: владельцы/superusers могут её обходить. [PostgreSQL: row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

Source/media parsers изолированы без production DB credentials, с read-only filesystem и CPU/memory/time limits. Fetch proxy разрешает HTTP(S), проверяет resolved IP и каждый redirect, блокирует private/link-local/loopback/metadata ranges, фиксирует endpoint против DNS rebinding. HTML/PDF/архивы/audio считаются вредоносными до parsing. Private source connector держит секреты вне модели.

Tool allowlist определяется ролью/TaskSpec, dispatcher повторно проверяет scope при исполнении. Источник/script не расширяет права, не добавляет tools, не запускает shell/SQL/email. Произвольный код из источника не исполняется даже если назван «формулой проверки»: CalculationRecord из 04 использует отдельный проверяемый execution contract и ограниченный вычислительный worker.

Полные prompts/responses нужны для диагностики, но чувствительны. Encrypted payload хранится отдельно от operational logs, доступ ограничен/аудируется, URLs краткоживущие. Метрики содержат IDs/error classes, не raw текст. Secrets вставляет adapter перед сетевым вызовом; archive хранит безопасный request body/config и key alias, без Authorization/credential headers.

Immutable не означает вечное хранение. Tombstone немедленно запрещает retrieval/новые calls/export; impact отзывает release eligibility. Purge удаляет originals, representations, embeddings, caches, trace payloads и производные тексты/медиа, содержащие удалённые данные. Остаётся минимальная nonsensitive audit запись; hashes с риском восстановления/сопоставления тоже удаляются/заменяются opaque IDs.

Purge учитывает object versions, exports/CDN, provider stored responses/files, backups. Неизвестный provider deletion status виден в job, не выдаётся за успех. Backups имеют конечный retention, tombstone log применяется до доступа после restore. Per-project key destruction допустим только при удалении всего проекта. При удалении одного источника ключ остальных данных сохраняется: немедленная невосстановимость требует per-object/per-artifact keys с проверенным backup key lifecycle, иначе действует явно указанный конечный срок хранения резервных копий. Редакция создаёт новый artifact; нельзя объявить исходник удалённым, сохранив его текст в trace.

## 13. Эксплуатация

API, workflow/model/source/audio workers развёртываются раздельно. Concurrency ограничивается по provider/project/type; один 90-минутный выпуск не занимает всё. CPU media и network/model pools масштабируются отдельно. Admission control показывает waiting с причиной, не «провал исследования».

Используем managed PostgreSQL с PITR, managed Temporal либо поддерживаемую HA-конфигурацию и object storage с checksum/versioning. Restore требует совместимых DB snapshot/WAL, object versions и Temporal history; reconciliation находит committed-but-not-dispatched и completed-but-not-adopted. Restore test завершает выпуск после аварии, а не просто открывает API.

Workflows закреплены за deployment version до завершения/явной миграции. CI воспроизводит сохранённые histories; incompatible orchestration меняется через versioning/patching. Adapter/prompt profiles имеют отдельные версии: prompt change не обязан менять workflow semantics, но меняет provenance. [Temporal: deployment versioning](https://docs.temporal.io/develop/python/workflows/versioning).

Технические метрики: queue age, activity errors, provider unknown outcomes, checkpoint age, CAS conflicts, duplicate adoption, missing blobs, index lag, stale eligibility, deletion backlog. Качество: recall/evidence validity/factual severity/narrative quality/audio defects по 04/05. Количество notes не показатель качества.

Runbook локального ремонта: сохранить proposal и исходный preview → записать actor approval exact hash → повторно сверить parent/request/budget → один durable TTS intent → сохранить receipt/decoded result → повторно сверить тот же parent ref/hash и blob перед splice → atomically опубликовать новую immutable version/map и оба join previews. Crash/resume не повторяет оплату или adoption; unknown остаётся unknown до явного reconciliation. Несовпадение parent/hash сохраняет результат отдельно и не вклеивает его в другую версию. Original master и старые версии доступны; replacement, все edits, shifted ranges и ещё не выполненный audition видны в экспорте. Секреты в previews/logs не попадают.

Автоматические signal checks и отчёт о повреждении не означают распознавание бессмыслицы. Не включать VAD либо экспериментальный quality score как скрытый detector/gate. Новый такой detector требует реального корпуса дефектов, negatives и qualification; текущий человеческий repair от него не зависит.

Стартовые targets для испытаний: control API p95 <1 с без provider latency; подтверждённое событие в UI ≤5 с; прекращение выдачи новых задач после cancel ≤5 с; committed artifact не теряется при worker crash. Эти значения требуют измерения. Полная latency тестируется отдельно для поддержанных 15/30/60/90-минутных language/format cases, без универсального обещания скорости.

## 14. Приёмочные сценарии

| Сценарий | Наблюдаемый результат |
|---|---|
| Crash между upload/DB | Нет broken ref; orphan найден; retry создаёт один логический результат |
| Crash после DB до event delivery | Outbox доставлен; duplicate event не повторил mutation |
| Provider завершился, сеть оборвалась | Unknown outcome виден; retrieve/replacement не дал два adopted результата |
| Researchers/editor race | Оба результата сохранены; CAS допускает одну интеграцию на head |
| Cancel до позднего TTS ответа | cancelled без нового ready; поздний ответ лишь diagnostic |
| Timestamp либо proposal без approval | Контекстный preview есть, TTS submit отсутствует |
| Proposal изменён после approval | Старое подтверждение неприменимо; до оплаты требуется новый exact approval |
| В submit/apply подставлен другой parent или изменились source bytes | Parent/hash conflict, нет вклейки в чужую версию; original/response сохранены, явная ветка от старого master разрешена |
| Local replacement длиннее/короче прежнего диапазона | Exact delta/map, один adopted master, suffix сохранён и сдвинут; оба joins требуют audition |
| Новый dossier во время writer task | Старый manifest закреплён; обновление через explicit handoff |
| Новый источник в supplied-only | Outside-scope отказ, corpus/context не расширен |
| Read tool завершился, next call отменён | Нет ложного receipt доставки модели |
| Script изменён после фактчека | Прежний report/acceptance не допускает новый release |
| D2 принят во время правки на P1/D1 | Production S2 под P1/D2 не принят. Согласованный P2/D2 позволяет повторное письмо и проверки |
| R зарегистрирован после допуска текста или пропущен в следующем author sidecar | Следующий preflight видит R по журналу ветки. Оригинальный допуск не обходит регистрацию |
| В stage direction записан ключевой вывод, реплики его не содержат | Проверка payload и отказов tool dispatcher подтверждает, что первый читатель имел доступ только к spoken_view и разрешённому brief |
| Evidence изменён, речь совпадает | Новый фактчек/eligibility, обоснованный reuse audio, новый manifest |
| Передан UUID чужого project | Нет payload/metadata leak или foreign retrieval |
| Tenant запросил published catalog profile | Разрешена exact config и safe eligibility; raw eval/другой tenant corpus недоступны |
| Candidate profile без qualification в production | Запуск отклонён; тот же candidate разрешён pinned evaluation case, но не создаёт userready |
| Ошибочный факт удалён из новой версии | Accepted dispositions снимают только её active quality edge; trace/privacy lineage сохранены |
| Discovery snippet без acquisition | RuntimeRef сохранён в manifest; нет фиктивного EvidenceSpan/ReadReceipt первоисточника |
| Backup restore после удаления | Tombstones применены до доступа; данные не вернулись в search/trace/export |
| Multilingual search под узким ACL | Recall сверен с exact corpus, ANN underfill диагностирован |
| Поле длиннее contract | Явная ошибка, raw response сохранён; нет sliced success |
| Deploy при активном выпуске | Replay проходит, run завершает/мигрируется без nondeterminism |

Harness сохраняет команды/refs, fault injection point, события и assertions. Эти проверки нужны для надёжности, но не заменяют продуктовый benchmark 04.

## 15. Реализационные пакеты

1. **Contract kernel:** canonical types, scoped refs/ConfigRefResolver, repository, blobs/hash/migrations/generated schemas. Готовность: полный round-trip смысла, запрет cross-tenant content refs и проверенный published-catalog config exception.
2. **Execution kernel:** commands/outbox/inbox, Temporal, task lifecycle, CAS/epoch, cancel/resume. Готовность: crash/retry/race scenarios без модели.
3. **Model/tool boundary:** adapters/profiles, invocation archive, context compiler, admissions/receipts, dispatcher. Готовность: fake-provider faults + реальные qualification calls каждой capability.
4. **Sources/retrieval:** порты 01, immutable indexes, language-aware hybrid retrieval, isolation/deletion. Готовность: retrieval и contamination cases.
5. **Domain workflows:** подключить 02–05 без переноса семантики в runtime. Готовность: полный episode trace с pinned dependencies.
6. **User completion:** UI/API, revision, playback, sources, export, recovery. Готовность: пользователь проходит весь путь без ручного управления tasks.
7. **Operations qualification:** deployment replay, load/cancel/restore/delete, drift canary, dashboards/runbooks. Готовность: испытания 04, не число endpoints.

До production проверить current provider retention/retrieve/cancel, context profiles на длинных русских текстах, exact/hybrid recall на датах/таблицах/именах, unknown outcomes, branch conflicts и restore удалённых данных. Эксперименты могут менять adapter/настройки, но не молчаливо ослаблять общий контракт.
