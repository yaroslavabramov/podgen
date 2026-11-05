# 01. Источники и доказательства

Статус: спецификация целевого продукта. Общие типы, версия выпуска и границы доверия заданы в [00-system-contract.md](00-system-contract.md). Модуль отвечает за то, **что именно прочитала система, откуда это взялось и как повторно проверить этот фрагмент**. Он не определяет истинность утверждения, достаточность исследования или качество истории.

## 1. Ответственность и границы

Входы: разрешённая область поиска из EpisodeSpec/TaskSpec, поисковый вопрос, URL или загрузка пользователя, сохранённая версия источника, запрос конкретного фрагмента. Выходы: оригинал, его проверяемое представление, точные EvidenceSpan, результаты поиска, события доступа/изменения и воспроизводимые ссылки.

Модуль обслуживает исследователя, редактора и независимого проверяющего через одинаковые read-инструменты. Исследовательская записка хранится в модуле 02; связь «фрагмент поддерживает вывод» — интерпретация автора записки, а не свойство исходного текста. Окончательный вердикт по утверждению сценария принадлежит модулю 04.

Обязательные правила:

- Оригинал сохраняется до извлечения текста. Ошибка извлечения не теряет скачанный материал.
- Поисковый snippet, заголовок, summary модели и перевод не становятся цитатой оригинала.
- Чтение автоматически создаёт пригодную для ссылки опору; модель не заполняет вручную реестры источников и доказательств.
- Полный материал и смысловые поля сохраняются без усечения. Пагинация выдачи и ограниченный preview явно маркируются.
- Поиск находит материал. Даже высокий retrieval score не означает достоверность или независимость.
- Source/evidence/data refs принадлежат тому же проекту и разрешённому scope. Совпадение URL или хеша не даёт доступа к чужому материалу. Исключение для точных опубликованных configuration refs из read-only catalog задаёт 00; оно не распространяется на содержание источников.

## 2. Предметные сущности

Все версии используют ArtifactRevision из 00. Ниже перечислены поля `payload`; ID, project_id, input_refs, producer, timestamps и content_hash находятся в общем envelope. `BlobRef` означает `{storage_key, sha256, byte_length, media_type}`; storage_key разрешается backend и не раскрывается модели. Публичное чтение выдаёт временную ссылку после проверки доступа. SourcePolicy — точный ref `Policy(policy_kind=source)` из PolicyBundle; extractor/render/retrieval settings — точные `ProcessingProfile` соответствующего profile_kind. Published catalog config разрешается только через ConfigRefResolver06 с проверкой subtype, публикации и квалификации; источники, representations, spans и оценки их происхождения остаются project-local.

### Source: идентичность произведения

`Source = {source_id, project_id, identity_key, identity_kind, created_at}` — неизменяемая идентичность документа/записи. `source_id` равен artifact_id его SourceRevision. Метаданные и адреса не используются как разрешение доступа.

`identity_kind`: `web_document | uploaded_document | dataset | recording`. URL — наблюдаемый адрес, а не доказательство уникальности. Параметры запроса сохраняются, кроме явно известных tracking-параметров; нельзя удалить query, меняющий версию, язык или содержание. Перевод, зеркало и перепечатка остаются отдельными Source с явными отношениями; повторное скачивание того же документа не создаёт новый Source.

### SourceRevision: что реально было получено

```text
SourceRevision.payload = {
  source_id, original_blob: BlobRef,
  requested_uri, resolved_uri, redirect_chain,
  acquired_at, response_metadata: {status, content_type, etag?, last_modified?},
  title?, creators: [], publisher?, language?,
  published_at?: {value, precision, basis}, modified_at?: {value, precision, basis},
  applicability?: {valid_from?, valid_until?, geographic_scope?},
  acquisition_kind: http | browser_snapshot | upload | connector,
  acquisition_record_ref,
  rights: {access_class, storage_policy_ref, publication_policy_ref},
  source_metadata_evidence: [], limitations: []
}
```

`acquired_at` никогда не заменяет дату публикации или период применимости. Даты из разметки сайта остаются наблюдением с `basis`; конфликт дат не разрешается выбором более свежей. Дата historical документа не делает его плохим ответом на исторический вопрос. Допустимая граница знания — `EpisodeSpec.temporal.knowledge_as_of`; время рассматриваемых событий — отдельный `EpisodeSpec.temporal.subject_period`. Режим historical_as_known допускает только известное на cutoff; retrospective может использовать более поздние сведения об этих событиях до knowledge_as_of. Повторный fetch после resume не сдвигает cutoff.

`AcquisitionRecord` — append-only operational запись: хранит запрос, цепочку переходов, hash полученных bytes, назначение получения, разрешённый scope и технический исход. Это не ArtifactRevision и не доказательство смысла документа. Authorization headers, cookies и подписанные URL не попадают в model context/обычные логи. Повторный fetch с прежними bytes создаёт новый acquisition/freshness record, но переиспользует SourceRevision. Изменение bytes создаёт новую revision. Исправление существенной описательной metadata также создаёт revision с прежним original_blob и явной причиной; старые ссылки сохраняются.

### SourceRepresentation: что извлекли из оригинала

```text
SourceRepresentation.payload = {
  source_revision_ref,
  representation_kind: html_text | document_layout | table | dataset | transcript | translation,
  content_blob: BlobRef, canonical_text_blob?: BlobRef,
  extractor: {profile_ref: ArtifactRef, name, version, configuration_hash, model_profile_ref?},
  normalization_version,
  blocks: [{block_id, kind, parent_id?, text_range?, original_anchor, warnings: []}],
  extraction_report: {coverage, missing_regions: [], reading_order_issues: [], warnings: []},
  derived_from_representation_ref?: ArtifactRef,
  language, is_transformation: boolean
}
```

`blocks` может храниться в индексируемой sidecar-таблице; её snapshot/hash входит в content_hash. `block_id` устойчив внутри этой representation, не переносится по предположению в другую. Нормализация извлечённого представления versioned: UTF-8, LF, документированный Unicode NFC, явные разделители блоков. Это явное преобразование в новую representation с сохранённым оригиналом; repository/hash layer06 строки не нормализует. Координаты `char_start/char_end` — индексы Unicode code points, полуинтервал `[start,end)`, не bytes и не JavaScript UTF-16 units. Сопоставление с оригиналом хранится отдельно.

`extractor.profile_ref` адресует точный ProcessingProfile(extraction), name/version/configuration_hash — его диагностическая projection, а не альтернативный источник конфигурации. RenderProfile в selector означает ProcessingProfile(render). Новый extractor, исправленный OCR или другой reading order создаёт новую SourceRepresentation. Оригинальный SourceRevision при этом может остаться прежним. Перевод — отдельное производное представление с ссылкой на оригинал; его нельзя показать как дословную цитату на исходном языке.

`coverage` имеет значения `complete | partial | unknown`, а не фиктивный процент. `complete` означает обработку всех обнаруженных регионов документа, не доказательство безошибочного чтения. OCR/ASR остаётся измеряемой гипотезой извлечения даже при полностью покрытых страницах.

### EvidenceSpan: точная адресуемая опора

```text
EvidenceSpan.payload = {
  source_revision_ref, representation_ref,
  selector: TextSelector | TableSelector | DatasetSelector | PageRegionSelector | TimeSelector,
  exact_text?: string, exact_text_hash?: string,
  media_anchor?: {blob_ref, selector},
  context_selector,
  extraction_warnings: [],
  transformation: none | ocr | asr | translation
}
TextSelector = {kind: text, block_ids: [], char_start, char_end, exact, prefix, suffix}
TableSelector = {kind: table, table_block_id, cells: [{row_id, column_id}], header_block_ids: [], footnote_block_ids: []}
DatasetSelector = {kind: dataset, record_ids: [], field_ids: [], original_record_locators: []}
PageRegionSelector = {kind: page_region, page_index, bbox, coordinate_space, render_blob_ref, render_profile_ref}
TimeSelector = {kind: time, track_id, time_base: {numerator, denominator}, origin,
                start_tick, end_tick, stream_offset_ticks, alignment_kind, transcript_block_ids: []}
```

`kind` — discriminator selector union. `page_index` начинается с 0; UI показывает page_index + 1. `bbox=[left,top,right,bottom]` в `[0,1]`, coordinate_space всегда `normalized_top_left` точного render_blob после применения сохранённого поворота. RenderProfile задаёт resolution, rotation transform и связь с PDF coordinates; другой render не меняет старый selector. Model-facing locator `{page_number: 2}` однозначно означает вторую страницу и преобразуется backend в page_index=1.

TimeSelector использует полуинтервал `[start_tick,end_tick)` и rational time base в секундах на tick. `origin=decoded_track_start`; track_id однозначен внутри сохранённой записи. `stream_offset_ticks` связывает начало декодированной дорожки с исходным container timestamp; для файлов без такого timestamp равен 0 с указанным decoder profile. `alignment_kind=decoder|asr_estimate|reviewed` отличает техническую координату от приблизительной границы распознанной фразы. Для millisecond representation time_base={numerator:1,denominator:1000}. Нельзя переносить интервалы на перекодированную/обрезанную запись без сохранённой time mapping.

TextSelector хранит quote и позиции одновременно: backend сверяет `exact == canonical_text[start:end]`. Prefix/suffix нужны для диагностики и визуального контекста, не для автоматического переноса цитаты в новую версию. Модель не вычисляет offsets: выбирает показанные block/selection aliases; backend возвращает точные координаты. Для произвольной подстроки инструмент принимает quote с ближайшим block alias и при неоднозначности предлагает найденные диапазоны.

Такой выбор опирается на подход W3C TextQuoteSelector/TextPositionSelector: цитата и положение решают разные задачи. Наш binding к неизменяемой representation дополнительно исключает незаметный сдвиг после обновления страницы. [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/#selectors).

EvidenceSpan без текста допустим для изображения графика или аудио. Тогда обязательны media_anchor и region/time selector; текстовое описание изображения хранится отдельно и не подменяет изображение. `exact_text` OCR/ASR совпадает с сохранённой транскрипцией, но это **не** гарантия совпадения с печатью/речью. Существенное число, имя или цитату проверяющий сверяет с изображением/звуком при extraction warning либо спорной поддержке.

В EvidenceSpan нет `is_true`, `supports_claim` или универсального `confidence`. Обнаруженная ошибка OCR создаёт исправленную representation и новый span; старый span не переписывается. Результат проверки «поддерживает с ограничением» хранится в VerificationReport, а не добавляется к исходной цитате.

### Происхождение и зависимость источников

```text
SourceRelation.payload = {
  left_source_ref: ArtifactRef, right_source_ref: ArtifactRef,
  relation: identical_content | mirrors | translates | republishes | cites | same_underlying_dataset,
  basis_refs: [], method: exact_hash | metadata | semantic_review,
  rationale?, reviewer_ref?
}
SourceFamilyAssessment.payload = {
  member_refs: [], common_origin_refs: [], relationship_refs: [],
  scope_description, unknown_relationships: []
}
```

SourceRelation и SourceFamilyAssessment — versioned artifacts registry00, владельцем схем является 01. Все member/source/basis refs разрешаются в точные revisions внутри проекта; relationship_refs адресуют SourceRelation. Assessment описывает зависимость для определённого вывода/набора данных через scope_description, а не навсегда классифицирует сайты. Исправление отношения/семейства создаёт новую revision и impact review; отдельная таблица — только индекс этих artifacts, не вторая изменяемая копия смысла.

Точный hash доказывает совпадение bytes; near-duplicate detector лишь предлагает отношение для проверки. Два разных домена не считаются независимыми. Газетная статья, пересказ пресс-релиза и сам пресс-релиз могут иметь общий исходный источник. Для конкретного вывода две статьи могут использовать один dataset, но разные методы анализа: семейство данных и независимость интерпретации фиксируются раздельно.

Нельзя навсегда присвоить сайту общий рейтинг истинности. Отдельные характеристики — происхождение, компетенция автора по вопросу, близость к событию, опубликованная методика, конфликт интересов, дата и возможность проверки. Итоговую применимость к утверждению оценивает модуль 04. Неизвестное происхождение отображается как неизвестное, а не как независимость.

## 3. Получение и извлечение

### Последовательность ingest

1. Проверить права, SourcePolicy, URL/upload ограничения и idempotency key; сохранить AcquisitionRecord в `requested`.
2. Получить bytes через изолированный egress или authorized connector; записать staging blob и hash. Проверить media type по содержимому, размер, checksum и целостность контейнера.
3. Атомарно зарегистрировать SourceRevision и ссылку на готовый blob; `acquired`. Незарегистрированные staging objects удаляет сборщик, с grace period для незавершённых попыток.
4. Запустить подходящий extractor в изолированном worker. Сохранить все извлечённые блоки, карту оригинала, отсутствующие регионы и параметры. `represented` либо `extraction_failed`; bytes остаются доступны.
5. Построить индекс только по зарегистрированной representation. Commit поискового snapshot и `indexed` отдельно; временное отсутствие в поиске не запрещает прямое чтение по ref.
6. Опубликовать admission в TaskRun и следующий InputManifest перед следующей модельной итерацией. Model-facing alias не перепривязывается.

Состояния ingest технические, не редакционные: `requested → fetching → acquired → extracting → represented → indexing → indexed`; терминальные причины `access_denied | unavailable | unsupported_media | extraction_failed | quarantined`. Повторяется конкретный этап. `represented` с `coverage=partial` не преобразуется в успешное полное извлечение.

### HTML

Статическое получение — базовый путь. Храним исходный HTML, доступные response metadata и извлечение. Trafilatura используется для main-content extraction, metadata и структуры с включёнными таблицами/ссылками; результаты адаптируются в наш стабильный block format. Возможность извлекать эти элементы описана в [Trafilatura Python usage](https://trafilatura.readthedocs.io/en/stable/usage-python.html). Библиотека не гарантирует полноту страницы: сравниваем main-content с доступным body и выдаём diagnostics при пропавших разделах/таблицах.

Исходный HTML и полный безопасный text projection остаются доступны независимо от main-content. Если основной документ создаётся JavaScript, browser worker сохраняет итоговый DOM snapshot и render. Запуск JS не даёт странице доступ к секретам, filesystem и внутренней сети; все subresource requests проходят тот же egress policy. Динамический snapshot имеет captured_at и ограничения воспроизведения; он не выдаётся за точные bytes первоначального HTTP response.

### PDF, изображения, сканы

Docling — исходный document-layout adapter; сохраняем страницы, таблицы, порядок чтения и provenance. Его `ProvenanceItem` содержит page/bounding box/char span, что позволяет строить обратную ссылку на страницу. [Docling document model](https://docling-project.github.io/docling/reference/docling_document/). Версии engine и OCR-моделей pinned в extractor profile; смена engine требует regression corpus.

Native text layer и OCR доступны как разные derivations. Скан, повреждённый text layer, формула, много колонок и сложная таблица требуют region/page review, а не уверенного слияния текстов. Если extractor не умеет отобразить нужный фрагмент обратно на страницу, такой фрагмент остаётся navigational preview до получения воспроизводимого anchor. Для таблицы сохраняются заголовки, единицы, примечания, merged-cell topology и исходная картинка; нельзя отдать исследователю голые числа без их контекста.

### Аудио/видео и наборы данных

Запись хранится целиком; ASR — отдельная representation с версией модели, языком, speaker labels, time ranges и неуверенными участками. Speaker diarization не доказывает личность говорящего. `inspect_evidence` позволяет воспроизвести исходный участок с соседним контекстом. Результаты ASR не используются как независимый второй источник по отношению к самой записи.

CSV/JSON/API response сохраняются в исходных bytes и `representation_kind=dataset`. Payload дополнен `dataset_schema={fields:[{field_id,source_name,type,nullable,unit?,timezone?}],null_rules,decimal_locale,record_identity_rule}`. Type выбирается из `string|integer|decimal|boolean|date|datetime|json`; исходное строковое значение сохраняется рядом с parsed value. Missing, explicit null и пустая строка различаются. Нельзя принять запятую за разделитель тысяч без declared locale или превратить неизвестную единицу в предположенную.

DatasetSelector адресует устойчивые record_ids/field_ids внутри точной representation. При отсутствии primary key record_id выводится из оригинального zero-based row index, а не из порядка после сортировки. `original_record_locators` хранит для CSV original row/column indexes и byte ranges, для JSON — JSON Pointer и исходный value; для paged API — exact response/page refs. SourceRevision включает snapshot времени запроса и параметры без секретов. TableSelector применяется к визуальным таблицам; DatasetSelector — к типизированным записям. Преобразование между ними сохраняет mapping, включая units/headers/nulls.

Расчёт — `CalculationRecord` модуля 04 с входными refs, формулой/кодом, параметрами и результатом. EvidenceSpan адресует исходные клетки, а не выдаёт вычисленный показатель за прямую цитату. Произвольное исполнение кода источника запрещено; инструмент вычисления исполняет только явно разрешённое действие в отдельном worker.

## 4. Поиск и чтение для модели

### Два режима поиска

`workspace` ищет только в разрешённых snapshot проекта; `web` ищет новые кандидаты в пределах source policy. Результаты поиска — DiscoveryHit внутри append-only SearchRecord, не SourceRevision/EvidenceSpan. При недоступности страницы остаётся честный discovery record; snippet нельзя автоматически использовать как evidence. Загруженный пользователем материал и найденный в web проходят одинаковую проверку представления.

```text
SearchRecord = {record_id, project_id, task_run_id, query, scope, filters,
                provider_profile_ref, searched_at, response_blob_ref, response_hash,
                hits: DiscoveryHit[], tool_execution_id, policy_ref}
DiscoveryHit = {hit_id, ordinal, locator, title?, snippet?, observed_metadata,
                candidate_artifact_ref?: ArtifactRef, rank_components, limitations: []}
```

SearchRecord и finalized ToolExecution адресуются `RuntimeRef` с record_kind=`search_record|tool_execution`; DiscoveryHit выбирается по hit_id внутри SearchRecord, отдельный ArtifactKind для него не вводится. Query/results/tool metadata включаются в `InputManifest.runtime_refs` через `InputAdmissionRecord.admitted_runtime_refs`. Их фактическую доставку показывают ContextAssembly, полный ToolExecution output и exact invocation request archive. Поисковому snippet не выдаётся ReadReceipt первоисточника; из него не создаются EvidenceSpan или DerivedPreview. Это верно и для workspace snippets: candidate_artifact_ref позволяет отдельно открыть закреплённую representation, но metadata выдачи сама её не заменяет.

Workspace retrieval объединяет lexical и multilingual embedding retrieval, затем reranking по вопросу. Исходный backend — PostgreSQL full-text/pgvector из 06. Индексы являются пересоздаваемыми проекциями; точные цитаты всегда читаются из pinned representation. Для русского, языков без пробелов, имён и коротких числовых запросов отдельно проверяются tokenizer, lexical recall и cross-language retrieval; один English stemming configuration не применяется ко всем языкам.

Единица индекса — связный блок с heading trail и ссылкой на соседние блоки. Размер оптимизируется на проверках поиска; chunk — навигация, не граница смысла. Запрос о причинности возвращает не только наиболее похожую фразу, но позволяет открыть аргументацию раздела. Повторяющиеся family hits группируются в выдаче; исходные результаты не удаляются.

Ranking хранит компоненты отдельно: lexical rank, vector rank, reranker score, temporal match, source-family relation. Модель видит краткое `why_returned` и ограничения, не декоративную вероятность истины. Фильтры периода применяются по корректному полю: событие/публикация/получение, неизвестные даты не исчезают молча.

### Публичные инструменты

Infrastructure IDs, scope, credentials, idempotency и access checks добавляет backend. Модель получает 5 предметных инструментов; индекс/DB/fetch internals ей не показываются.

| Tool | Model-facing вход | Результат и атомарное действие |
|---|---|---|
| `search_sources` | `query`, `scope: workspace|web`, `filters?`, `purpose?`, `cursor?` | Hits с source/discovery aliases, runtime snippet, датами с basis, family hints, why_returned, next_cursor. Сохраняет SearchRecord и выдачу; EvidenceSpan/DerivedPreview/ReadReceipt не создаёт |
| `acquire_source` | `discovery_alias` или `url`, `purpose?` | Получение/извлечение с admission; возвращает source/representation aliases, состояние, ограничения, доступные чтения. Уже сохранённую revision переиспользует |
| `read_source` | `source_alias|representation_alias|evidence_alias`, `locator?`, `context: section|neighbors|full`, `cursor?` | Дословные блоки, автоматические EvidenceSpan aliases, original anchors, continuation. Full означает последовательный доступ ко всему, а не обещание уместить документ в одном ответе |
| `inspect_evidence` | `evidence_alias`, `include: original|context|provenance` | Исходная страница/аудио/таблица или окружение, цепочка derivations, warnings; delivery candidate для receipt |
| `compare_sources` | `left_alias`, `right_alias`, `mode: version|origin` | Текстовый/metadata diff или известное происхождение, точные changed regions; отсутствие semantic verdict |

`read_source(source_alias)` использует representation, закреплённую за alias в manifest; не «последнюю». Для выбора иной representation ответ предлагает явные aliases. `locator` принимает heading/block/page/table/time или quote+block. Пустой locator возвращает оглавление и релевантное начало, а не случайный chunk. При `context=full` ответ содержит `returned_ranges`, `total_extent`, `has_more`, `next_cursor`; все доступные диапазоны можно открыть без потери данных. Cursor подписан и привязан к exact revision.

Каждый инструмент возвращает канонический ToolResult из 06: `{ok:true,data,warnings:[],continuation:null|{cursor,...}}` **либо** `{ok:false,error:{code,message,retryable,recoverable_actions:[],details_ref:null|ArtifactRef}}`. Success и error взаимоисключающие. `recoverable_actions` содержит разрешённые имена действий с объяснением, не произвольные команды. Infrastructure IDs остаются в trace. Основные коды: `outside_scope`, `access_denied`, `unsafe_destination`, `unavailable`, `unsupported_media`, `extraction_incomplete`, `ambiguous_selection`, `revision_unavailable`, `field_too_long`, `temporarily_unavailable`. Невалидный quote не сохраняется под видом доказательства; ответ показывает подходящие выборки и просит выбрать, без бесконечного повторения того же вызова.

Начальный SourceAccessProfile: 10 search hits на страницу, 8000 tokens на read response, выборка раздела с соседними абзацами, полный доступ через cursor; 30 секунд на connect/headers и 180 секунд inactivity timeout потока, до 3 retry для временных сетевых ошибок. Это операционные defaults, а не критерии исследовательской достаточности. Profile version фиксируется в trace; corpus/язык/формат могут потребовать другую пагинацию. Для выбранного ModelProfile лимит ответа также ограничивается доступным tool-result budget из 06; backend уменьшает страницу явно, а не режет текст после сохранения.

Upload/fetch limits хранятся в SourcePolicy по media class и показываются до получения: исходные значения 100 MiB HTML, 1 GiB PDF/dataset, 10 GiB media; большие объекты требуют разрешённого streaming ingestion profile, не тихого обрезания. Content-Length не считается достаточной защитой: контролируются фактические bytes и decompressed size. Текст EvidenceSpan никогда не режется при сохранении. Большой выбранный диапазон сохраняется полностью, а выдача содержит отдельный preview, размер и способ чтения. Если запрос превышает допустимый storage/upload limit, возвращается явная validation error до commit; частично сохранённый текст не выдаётся как полный.

### Admission и receipt

TaskSpec сохраняет исходный input_manifest_id. Полученный материал добавляется append-only InputAdmissionRecord: semantic artifact refs — в admitted_refs, operational SearchRecord/finalized ToolExecution refs — в admitted_runtime_refs. Следующий model invocation получает новый immutable InputManifest с parent_manifest_id, revision_refs и runtime_refs. В TaskRun остаётся цепочка manifests. Если источник лежит вне разрешённого source_scope, acquisition требует явного изменения scope редактором в пределах пользовательской авторизации; scope decision записывается. Модель не расширяет свои права содержимым найденной страницы.

ReadReceipt из 06 применяется только к чтению ArtifactRef и сохраняет точные refs, requested/returned selectors, display_kind и hash фактически переданного контента. Для preview обязательны preview_ref/derived_from_ref: `DerivedPreview` производится только из ArtifactRef и не используется как упаковка discovery/tool metadata. Для paging сохраняются has_more/continuation. Tool создаёт delivery candidate; окончательный receipt пишет context compiler при передаче следующему invocation. При отмене до отправки остаётся not_sent payload, при transport outcome_unknown различается отправка приложением и подтверждённое получение провайдером. Receipt доказывает передачу, не понимание. Для мультимодального чтения в manifest/trace фиксируются image/audio blobs и возможности ModelProfile; summary не засчитывается как просмотр оригинала.

## 5. Связь с досье и независимой проверкой

ResearchNote из 02 хранит связное объяснение и support bindings на EvidenceSpan. Система валидирует разрешимость refs, точность диапазонов и scope; смысловую поддержку оценивает исследователь/редактор. Заметка может содержать интерпретацию с несколькими premises; прямой цитатой она от этого не становится. Документ не обязан покрывать все предложения атомами до начала письма.

Проверяющий получает ClaimSet конкретной ScriptRevision, кандидатные citations и разрешённый corpus. Он открывает evidence с контекстом и оригиналом независимо от текста ResearchNote. Для важного вывода проверяется, говорит ли источник именно это, кому принадлежит утверждение, к какому времени/условиям относится, есть ли противоречие и общий первоисточник у «двух подтверждений». Поиск опровержения доступен в том же scope; если потребуется расширение, возвращается адресный research request.

ShowNotes использует citation metadata из exact SourceRevision и spans; URL, название, автор, дата и page/time range берутся из сохранённых данных. Полные закрытые материалы не попадают в публичные show notes. Доступная пользователю source viewer показывает версию, происхождение, страницу/временной диапазон и оговорку OCR/перевода.

## 6. Изменения, устаревание и удаление

SourceRevision и SourceRepresentation не меняются. Отдельные события `source_refreshed`, `source_corrected`, `source_retracted`, `representation_corrected`, `access_revoked`, `content_deleted` обновляют доступность и создают impact record. Metadata-only изменение имени издателя и изменение фактического числа не должны запускать одинаковую переделку: технический diff отмечает области, семантический impact review определяет затронутые выводы. Пока impact не разобран, связанный допуск не считается автоматически пригодным для нового выпуска.

Новая версия источника не подменяет snapshot на `EpisodeSpec.temporal.knowledge_as_of` и не отзывает его пригодность автоматически. Для historical_as_known допустимая прежняя версия может оставаться основанием; correction/retraction оценивается по policy и temporal mode, чтобы не смешать прежнее знание с поздним. Для current перед release запускается freshness policy по изменчивости утверждений, а не единый TTL для всех документов. Она проверяет применимость/corrections, но не передвигает knowledge_as_of: рассказ о более позднем текущем состоянии требует новой EpisodeSpec revision. subject_period при этом остаётся отдельным временем описываемых событий. Повторная проверка доступности URL не доказывает актуальность содержания.

Происхождение: `SourceRevision → SourceRepresentation → EvidenceSpan → ResearchNote/VerificationReport → ScriptRevision → ReleaseManifest`, плюс прямые factcheck/evidence и ShowNotes/source-metadata связи. `input_refs` и lineage сохраняют всю историю; **active quality dependencies** (`uses_content`, `uses_order`, `applies_policy`, `renders`, `verifies`) отдельно определяют текущую eligibility по 06. Доступность в manifest сама не доказывает смысловую зависимость; фактически показанные содержательные inputs учитываются консервативно до проверки.

Автор не снимает invalidation удалением ref из списка. Повторную проверку exact новой revision проводит владелец соответствующего артефакта по [общему контракту 00](00-system-contract.md#7-версии-активные-зависимости-и-исправления) и [runtime06](06-platform-and-runtime.md#11-invalidation-и-reuse). Для ScriptRevision основанием служат принятые `VerificationReport.dependency_dispositions`04; для ResearchNote — EditorialDecision и IntegrationEvent02; для ShowNotes — ShowNotesReview04; для audio — AudioQA05. У собранного кодом DossierSnapshot действующие связи выводятся из exact refs текущего payload, а прежняя версия остаётся lineage. Runtime записывает DependencyRevalidationEvent и пересчитывает active edges по допустимому основанию владельца: retained, replaced, removed либо lineage_only. Удалённое/заменённое основание не блокирует исправленный результат навечно, но trace сохраняется; допуск не переносится на другие артефакты и версии автоматически. ReleaseManifest immutable; пригодность старого ready хранится отдельной eligibility projection. Текст и аудио пересоздаются после решения о нужном изменении, а прежний approval не переносится автоматически.

Удаление по retention/access policy — отдельный workflow: сначала tombstone и запрет новых чтений, затем удаление originals, representations, chunks, embeddings, previews, cached model inputs и производных копий согласно политике 06. Privacy/access revocation использует **полную provenance/copy closure**, а не только active quality edges; editorial disposition не может отменить purge или восстановить отозванный доступ. Retained audit содержит только допустимые идентификаторы/причину/времена; он не хранит удалённый текст «для воспроизводимости». Backups и provider-side copies учитываются в общем retention plan. При недоступности необходимого evidence новый выпуск блокируется до замены/повторной проверки; UI честно показывает потерю проверяемости. Уже внешний опубликованный выпуск не считается автоматически удалённым — это отдельная публикационная операция.

## 7. Надёжность и безопасность

| Сбой | Поведение |
|---|---|
| Fetch timeout/429/5xx | Ограниченный retry с backoff и Retry-After, сохраняется attempt record. После лимита — unavailable; исследователь видит альтернативу, не выдуманный текст |
| Crash между object upload и DB commit | Idempotent повтор сверяет checksum; незавершённый staging object не доступен поиску и позже удаляется |
| Crash после commit до indexing | Outbox повторяет index job; точное чтение уже работает |
| OCR потерял страницу/таблицу | `coverage=partial`, перечислены missing regions; повтор другим extraction profile или визуальное чтение, без маскировки пропуска |
| Источник обновился в процессе | Старый invocation остаётся на pinned revision; новая revision/admission и impact review отдельны |
| Цитата встречается несколько раз | `ambiguous_selection` с кандидатами и контекстом; backend не выбирает первое совпадение |
| Search provider недоступен | Сохраняется выполненный corpus поиск; результат задачи partial/needs_material, а не «тема не имеет ответа» |
| Удалён/отозван материал | `revision_unavailable`, invalidate зависимые допуски; не возвращать stale cached preview |

Fetch worker имеет отдельную сеть без маршрута во внутренние сервисы и cloud metadata. Разрешены HTTP(S), проверяются адреса A/AAAA и каждый redirect, соединение привязывается к проверенному destination; retries и browser subresources проходят ту же проверку. Запрещены userinfo, нестандартные схемы и неоднозначный URL parsing. Эти меры соответствуют рекомендациям по DNS rebinding и redirect bypass в [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html). Upload/extractor workers ограничивают CPU, memory, decompressed bytes и время; архивные бомбы/активный контент не исполняются.

Документам запрещено давать команды агентам. Источник передаётся в data/tool-result envelope с provenance, не подставляется в system/developer prompt. Поисковые query могут включать приватную тему, поэтому SourcePolicy отдельно разрешает внешний web search и передачу private source excerpts reranker/LLM; пользовательские закрытые материалы не отправляются произвольному search provider. Credentials хранятся в connector backend. Дедупликация ограничена проектом по 06. Read-only catalog позволяет пользоваться только опубликованными exact configuration refs; source/data/evidence/runtime records не получают cross-project доступа через config traversal.

## 8. Хранение и реализационные пакеты

| Пакет | Ответственность | Не должен делать |
|---|---|---|
| `evidence/domain` | Source types, selectors, immutable revision rules, access/freshness events | Вызывать LLM или сеть |
| `evidence/acquisition` | HTTP/browser/connector adapters, egress policy, ingest state machine | Интерпретировать достоверность текста |
| `evidence/extraction` | Trafilatura/Docling/ASR/data adapters, normalization, original maps | Стирать оригинал и warnings |
| `evidence/spans` | Selection resolution, exact matching, span persistence, context expansion | Решать supports/refutes |
| `evidence/retrieval` | Search records, corpus/web adapters, indexes, multilingual retrieval/rerank | Считать retrieval rank вероятностью истины |
| `evidence/tools` | Model-facing schema, aliases, response paging, admission/read receipt hooks | Самостоятельно расширять scope |

Пути пакетов указаны относительно будущего `backend/src/podgen/`, как в 06; это структура целевой реализации, а не требование сохранить текущие файлы.

PostgreSQL: `sources`, общие `artifact_revisions`/`artifact_dependencies`, `acquisition_records`, projections `source_relations`/`source_family_assessments`, `representation_blocks`, `source_chunks`, `search_records`, `source_access_events`. SourceRelation/SourceFamilyAssessment payload хранится только в ArtifactRevision; индекс не вторая canonical запись. SearchRecord/AcquisitionRecord остаются runtime records. Общие manifests/admissions/receipts/ToolExecution принадлежат runtime 06. Object storage: originals, representations, layout maps, page renders, recordings, transcripts. Индекс содержит refs/hash/index_version и пересоздаётся; он не источник истины для citations.

Операции с blob и DB не изображаются одной распределённой транзакцией: staged write → checksum → DB commit + outbox → projection jobs. Idempotency key включает project, acquisition intent и external request identity; extractor job key включает source revision, extractor profile и normalization version. Повтор одной попытки не создаёт дублирующие revisions; намеренный новый fetch остаётся отдельным AcquisitionRecord.

## 9. Сквозной пример

Условный учебный документ, не факт о реальном учреждении: исследователь выясняет, почему город изменил расписание транспорта. Web hit содержит фразу о пилоте; он ещё не доказательство.

1. `acquire_source(discovery_alias="d3")` сохраняет PDF `src7@r1`; page 2 содержит протокол, extractor создаёт `rep7@r1` и отмечает плохо прочитанное число.
2. `read_source("rep7", locator={page_number: 2}, context="section")` возвращает раздел с `ev12`: «Тест длился 12 недель». В span записаны actual refs, text range, page_index=1/bbox и `transformation=ocr`; модель использует короткие aliases.
3. `inspect_evidence("ev12", include="original")` показывает страницу. На изображении читается «12»; receipt сохраняет image hash. Если OCR дал «72», создаётся corrected representation и новый span, не patch старого.
4. Автор записки сохраняет: «Длительность пилота известна; причина окончательного изменения расписания в этом протоколе не установлена». Support binding первой части указывает на ev12; вторая часть — ограничение найденного материала, не доказательство отсутствия причины в мире.
5. Второй сайт воспроизводит тот же протокол. `compare_sources(mode="origin")` показывает republication relation. Он не считается независимым подтверждением.
6. Сценарий «Город изменил расписание, потому что пилот длился 12 недель» получает unsupported causal link при независимой проверке. Точное совпадение числа не спасает причинный вывод.
7. Найденное последующее решение содержит объяснение причин. Его получение создаёт admission и новый invocation manifest; редактор интегрирует новую записку в DossierSnapshot. Предыдущие refs и trace сохраняются.

Минимальная сохранённая опора после шага 2, в диагностическом alias-виде:

```json
{
  "source_revision_ref": "src7@r1",
  "representation_ref": "rep7@r1",
  "selector": {"kind": "text", "block_ids": ["p2.b4"], "char_start": 0, "char_end": 21,
               "exact": "Тест длился 12 недель", "prefix": "", "suffix": ""},
  "exact_text": "Тест длился 12 недель",
  "transformation": "ocr",
  "context_selector": {"block_ids": ["p2.b3", "p2.b4", "p2.b5"]},
  "extraction_warnings": ["review_numeric_token_against_page"]
}
```

В wire/persistence вместо строковых aliases используются ArtifactRef, а в полном span присутствуют envelope, exact_text_hash и page anchor. В примере char range относится к canonical text тестовой representation, начинающейся с показанного блока; offsets реального документа вычисляет backend.

## 10. Приёмка и эксперименты

Обязательные детерминированные проверки:

- Round-trip SourceRevision сохраняет исходные bytes; длинный смысловой текст либо полностью доступен, либо запись отклонена до commit.
- Один и тот же текст с emoji/combining characters, несколькими цитатами и переносами разрешается одинаково в backend/UI; selectors не сдвигаются при новой extractor version.
- HTML, PDF text layer, скан, таблица с единицами, запись и CSV имеют открываемый original anchor. Ошибки OCR/ASR видимы.
- Один fetched URL после изменения bytes даёт новую revision; прежний EvidenceSpan воспроизводится по старой до её разрешённого удаления.
- Недоступный URL/snippet не становится evidence/DerivedPreview и не получает ReadReceipt; SearchRecord/ToolExecution admitted через RuntimeRef, delivery воспроизводим по request archive. Два mirrors не считаются двумя независимыми источниками.
- Изменение scope, admission и реально показанные данные воспроизводимы из manifests/receipts; alias не меняет ref внутри task.
- SSRF redirects/DNS rebinding, hostile HTML, oversized/decompression payloads, cross-project access и prompt injection не обходят права.
- Crash в каждой границе upload/commit/index повторяется без потери bytes, дублей и ложного success. Удаление убирает retrieval/preview/cache copies и делает старый ref честно unavailable; lineage_only disposition не сохраняет запрещённую копию.
- Новый источник после historical cutoff не подменяет temporal.knowledge_as_of; принятое исправление зависимости снимает только active quality edge exact проверенной revision. Published catalog ProcessingProfile доступен по разрешённому ref, чужой источник через него недоступен.

Качество extraction/retrieval проверяется на отдельном corpus: многоязычные страницы, исторические документы, короткие имена/числа, отрицания, сноски, сложные таблицы, плохой OCR и общие первоисточники. Эксперты размечают нужные evidence ranges и опасные искажения. Метрики: recall нужного раздела, сохранность чисел/отрицаний/единиц, воспроизводимость citations, число критических extraction omissions, достижение корректного ответа при чтении найденного материала. Универсальный порог similarity не задаётся.

Сравниваются extraction adapters и retrieval profiles на одинаковых snapshots. Порог достаточности, допустимые previews и size defaults калибруются по этому корпусу и end-to-end проверкам 04. Запуск новой библиотеки без такого сравнения не считается улучшением. Модуль готов к интеграции, когда фактическая проверка может открыть каждую использованную опору и её контекст, а любой технический отказ остаётся различимым от содержательного отсутствия ответа.
