> История промежуточного ревью. Итоговый статус и ограничения зафиксированы в README.md этого отчёта.

# Research corpus: editorial/runtime acceptance review

13 ноября 2026. Read-only по production, без model API и тестов. Основание: последний turn исследовательского чата `01a1b59f-b8ea-76b3-8473-fba1e06bc48b`, HEAD `9711a44c`, `tmp/research-corpus-design.md` (SHA-256 `a771b8989cc28c024953ce1a40bea9ec1cb36fff3871b3c419a5a6dd8ac66da4`). Это review предложения, не приёмка ещё не написанной реализации. Позднее в working tree появились source_id-запрос, catalog_version, отдельный resume_state и X/R-reader; их наличие отмечено ниже, поведение ещё не проверено тестами.

**Направление верное:** тот же порядок ролей и две переписи; завершать исследование внутри текущей проверки, а не отдавать автору технически оборванный аудит. Не нужен новый редактор перед черновиком, обход всех ссылок моделью или gate «все claims supported». Получение всех ссылок и выборочное содержательное чтение — разные работы.

## Что прочитано и что меняется

Полностью прочитаны восемь `runtime/instructions/*.md` и соответствующие `*.preview.json` из `evals/reports/2026-11-13-hosted-planning/runtime/`: plan, plan-review, plan-review-revised, plan-revise, draft, reader-review, fact-review, revise-1. Все полные instructions побайтно совпали с полем instructions соответствующего сохранённого preview. Payload разобран целиком; script/feedback в этих preview — fixtures, не новые live outputs. Дополнительно прочитаны полные фактические recheck instructions и trace старого live baseline, текущие adapters/views/tool schemas, source corpus/search/index, reading_cycle и новый `sources/material_requests.py`.

| Граница | Сейчас | Требуется только техническое изменение |
|---|---|---|
| Plan / plan review / plan revise | assignment, TOC, весь source_catalog | assignment + полный TOC + навигация в paginated catalog; literary body прежний |
| Draft | принятый story, полные selected M, catalogs | полный выбранный материал сохранить; большой каталог вынести за tool; не добавлять новую роль |
| Fact / recheck | script, remit, bibliography, E/H audit context, read tools | тот же предмет проверки; acquisition/research и context continuation внутри неё |
| Revise | готовые required tasks, P passages, полные M | выдавать после завершения текущей проверки; контракты смысловых правок не менять |
| Cold reader | только script/audience/format/genre; tools=[] | не передавать sources, researcher notes или чужой factual audit |

Исходный проверенный путь `source_tool_contract.py` задавал 12/24/forced final; `wire_budget.py` — 65 536 tokens/256 KiB; retrieval v1 — 64 docs/2M chars/12k chunks и eager arrays. В исходном reading_cycle `research_requests` заканчивают цикл как needs_material; этот отдельный путь тоже нужно разрешать внутри проверки. Новая версия должна обходить эти технические пределы новым pin, сохраняя старые manifests. Это не исправляется увеличением default page.

## Уточнить дизайн до реализации

1. **Полное сохранённое состояние ≠ полный inline resume payload.** Исходный P1 принят в schema: у WorkingState теперь отдельно state и resume_state. Приёмка C2–C5 должна подтвердить, что в новое окно входит compact поле, full notes сохранены без обрезки и доступны через history. Наличие полей само по себе не доказывает корректный checkpoint/replay.
2. **[P1] Eager restore возвращает корпусную загрузку.** `restore()->tuple[SourceAdmission]`, где каждый admission содержит полный text, потенциально собирает все новые документы в памяти заново. Нужны paged/iterable metadata + immutable document reader либо иной доказанно bounded путь; batching embeddings не устраняет eager tokenization/text accumulation. Полные bytes/text остаются в storage.
3. **Разделение supplemental research принято в prototype.** Координатор подтвердил X1 отдельно от sources; появился `read_research_result`, возвращающий R. Это снимает исходный риск смешения типов на уровне замысла. Приёмка E1/A3 должна подтвердить отдельно сохранённый full synthesis, provenance и source admissions; resolved по-прежнему не означает подтверждённость тезиса.
4. **[P1] Явная граница завершения semantic review.** В `MaterialResolution.result` нужны проверяемые resolved/pending/unavailable, durable handoff/attempt refs и corpus revision. Pending, context checkpoint и operational stop не порождают editorial final и не вызывают revise. Существующие structured research_requests/material requests также должны попадать в этот путь, а не обходить его terminal needs_material. После получения ответа продолжает тот же проверяющий.
5. **Actionable catalog: исходный P2 принят в schema.** Теперь request_source принимает offered source_id либо URL; source_catalog добавляет reported_url по флагу, list_sources — catalog_version. Приёмка I3/A2 проверяет реальные страницы и запрос известного S без догадки о URL. Ещё проверить immutable policy known S→новая representation: ссылки отчёта достижимы, старые E hash/ranges остаются воспроизводимы, IDs не перенумерованы.

## Точные acceptance cases

Cases — для адресных offline integration tests и последующего чтения реального run; здесь ничего не запускалось. Runtime валидирует операции/привязки и pending state, а не выносит содержательный verdict по числу чтений.

| ID | Сценарий | Проверяемый результат |
|---|---|---|
| F1 | Нужная проверка завершается только после turn 13 и call 25; модель затем сама возвращает review | Все операции выполнены; нет forced structured final, скрытого 12/24 cap или автоматического keep/revise |
| F2 | На незавершённой проверке срабатывает явный time/token/cost guard, cancellation либо сбой checkpoint | Durable unfinished state + точная technical reason; без final review/author assignment и расхода переписи; resume с того же вопроса |
| F3 | Fact requests конкретный отсутствующий документ: pending → resolved | До resolution автор не вызывается; тот же script_hash/review возобновляется, документ прочитан, затем собраны все необходимые tasks; две переписи не расходуются на ожидание |
| F4 | Документ окончательно unavailable; проверяющий выбирает достаточный bounded account либо необходимый вопрос остаётся неразрешимым | Модель решает revise/keep/needs_material по контексту; unavailable не становится false. Не повторяется бесконечно та же исчерпанная операция |
| F5 | Выбранная история требует части корпуса; прочие документы не нужны, нет незавершённых необходимых операций | Допустимо закончить без чтения всех M/S и без 100% citation/receipt quota. Incidental unchecked не автоматически блокирует; незавершённая необходимая работа не маскируется keep |
| F6 | Во время pending меняется script/plan или приходит новая существенная evidence version | Старый final не применяется к новому input; hash/version mismatch явный. Прежние applicable решения сохранены, зависимые пересмотрены той же ролью |
| C1 | Input >65 536 tokens или >256 KiB, но native input count + profile output reserve помещаются в реальное окно | Новый research contract допускает запрос. Проверяется полный native request с instructions/tools/schema/history; отдельный старый ceiling не применяется |
| C2 | Следующий запрос с reserved output уже не помещается в actual model context | Операционный checkpoint до переполнения; segment restart с task/remit, compact resume state, workspace/history refs и pending work. Не возвращать review ради освобождения окна |
| C3 | В full notes длинный rationale, точные цитаты с CRLF/астральным Unicode, positive/negative checks и следующий next_start | Full записи побайтно/по hash сохранены, compact поля отдельные; после resume доступны исходные значения, uncertainty, evidence premises и позиция чтения. Никакого persistence truncation |
| C4 | После rotation текущий read E1 уже использован; есть H1 прежнего audit, R receipt и незавершённый tool result | Нет переименования/коллизии aliases. R и notes не становятся E/H. Pending result доставляется по исходному intent; архивный receipt открывается с исходным ref, без нового fetch; новый E создаётся только для фактической новой доставки source passage |
| C5 | Crash после tool intent, после fetch, после result persistence, после checkpoint commit и перед segment selection; отдельный ambiguous paid submit | Replay использует durable operation/result/selected segment. Нет второго fetch/paid generation для известного завершения; ambiguous submit остаётся unresolved, не слепо повторяется |
| C6 | Cold reader получает context/history navigation либо чужой ID передан в read_work_history | Доступ только к собственному разрешённому task/history; нет источников, чужих reviews, секретов или hidden reasoning. Source text в истории остаётся данными, не системными инструкциями |
| E1 | Report утверждает размер/цену, search preview повторяет его; source — другой объект, abstract либо поздний пересказ | M/R, preview, research answer, workspace и acquisition receipt не считаются source evidence. Read_source даёт точный документ/representation/hash/range; secondary не переименовывается в primary |
| E2 | После дополнительного acquisition изменилось представление документа | Старый E/hash/range и вывод по нему воспроизводимы; новое чтение явно связано с новой version. Partial/challenge/fallback имеют честные coverage/status, не тихое available/full |
| I1 | >64 docs, >2M chars, >12k chunks; контрольный уникальный passage в последнем batch/документе | Все документы индексированы и доступны; контрольный поиск находит tail. Tokenization/embedding/DB writes bounded batches, durable progress; partial index не объявлен полным и не даёт окончательного no-hit |
| I2 | Много search results, несколько страниц, crash и повтор query/cursor | Сохранены query/scope/corpus/index revision и порядок страниц, без пропусков/дублей. Top-k candidate pool не выдаётся за полный перечень всех совпадений или доказательство отсутствия факта |
| I3 | Catalog больше страницы, содержит unavailable/not_acquired и sparse S; затем acquisition append | Initial prompt не содержит весь каталог. Одинаковые filter/cursor дают тот же snapshot; обновление явно версионировано/возвращает stale cursor. Все статусы и actionable URLs доступны, IDs не сдвигаются |
| A1 | Все reported URLs: duplicates, redirect aliases, markdown Sources и native occurrences, включая ссылку за прежним ручным набором 20 | Полный lossless inventory; dedup сохраняет все occurrences/aliases и не сливает разные документы по одному hostname. Для каждого unique target есть durable outcome, включая failures; доступное переиспользуется |
| A2 | request_source с URL уже известного missing S44 либо нового конкретного документа вне исходного списка | Resolver получает точный target/reason; deterministic operation_id, immutable admission/revision, явная связь с reported S. После restore документ searchable/readable; старый frozen packet не переписан |
| A3 | Нужен новый исследовательский вопрос, не просто загрузка известного URL | request_research сохраняет полный вопрос в research layer; pending продолжает тот же review. Ответ остаётся synthesis, source admissions читаются отдельно. Не запускается заново общий ресёрч и не появляется новый редактор |
| A4 | План использовал ответ X1 как выбранный дополнительный исследовательский материал | Зафиксировано, как он передаётся автору полностью и сохраняет связь с источниками. Нельзя подставить X в закрытый enum M или оставить автору только пересказ планировщика. Это не превращает X в E |
| B1 | Исторический v1–v6 manifest и новый v7 проходят prepare/resume/export | Старые pins/schema/model/request semantics воспроизводятся; новые tools/paging/context semantics включаются только новым pin. Literary text, models/order и две переписи не изменены |

## MUST change и full prompt diff без дублирования

В `tmp/research-corpus-instruction-review/` зафиксированы полные before/after девяти фаз и tool schemas. **after** — actual working-tree adapter поверх сохранённых полных baseline instructions; **proposed-after** — те же тексты с рекомендуемым минимальным v7 block. Это offline assembly, не новые отправленные model requests.

- `instructions-and-tools.diff`: existing → текущий actual v7, включая полные схемы tools.
- `runtime-operations-minimal.diff`: точная замена только нового v7 BODY, **308 → 189 слов**.
- `minimal-v7-runtime-block.txt`: готовый English block для developer.
- `full-minimal-instructions.diff`: existing → полный рекомендуемый результат по всем фазам.
- `manifest.json`: SHA-256 before/actual/minimal и проверки неизменности literary prefix.

**Найденное дублирование:** actual v7 BODY снова объясняет R/E/H vs report, не-доказательность previews, unavailable≠false, уже изложенные в existing research COMMON. Рекомендуется сохранить v1 COMMON дословно и сократить **только новый BODY** до операций: paginated catalog; request_source/research до завершения нужной проверки; X reader; full state/compact resume_state; history navigation; checkpoint/pending вместо verdict. Не менять оба блока и не создавать второй набор literary требований.

**MUST в минимальном block:** до unsupported на основании отсутствующего identified document вызвать request_source; для нового адресного вопроса — request_research; после pending продолжает тот же assessment; до завершения необходимого исследования final repairs автору не выдаются. Это не требует чтения всего корпуса и не отменяет supported bounded account. Остальные роли, их schema, модели, литературные части и две переписи прежние. Cold reader целиком unchanged.

**MUST в runtime:** существующие structured research_requests/material requests должны попадать в тот же продолжимый путь, что и tools. Один новый prompt не исправляет прежний terminal needs_material. Ответ X не является source admission; его полный выбранный контекст должен дойти до автора (A4).

Отдельно перед live проверить pin полного v7 operational text: bind сейчас вызывает runtime_instructions.attach. Новый BODY должен воспроизводиться из frozen version/text/hash при preview/resume, а не меняться из-за последующей правки текущего модуля; повторное attach не должно удваивать блок. Это проверка immutable assembly, не предложение менять содержание.

## Независимые regression tests

Добавлен только `backend/tests/test_research_navigation_contract.py`: 8 test functions / 16 parameterized cases, без БД, model API и acquisition. Проверяются полный historical v5/v6 request replay с закреплёнными hashes instructions, v7 navigation вместо inline catalog, full selected M и story автору, изоляция cold reader, factual navigation, status/URL/ID catalog paging и stale version, запрос missing S по ID, original-report/supplemental R vs source E и P handoff. Ruff и syntax check выполнены; **pytest не запускался**. Developer должен включить файл в единый согласованный набор, результаты до запуска не заявлены.

## Приёмка нового полного longitude run

До paid run: сравнить полный actual instructions/payload/schema/tool contract с этим baseline; назвать изменяемые решения агента — поиск каталога, адресный запрос, checkpoint/resume, момент завершения проверки. После: прочитать план и отдельную рецензию, полные первый/последний scripts, все reviews и trace. Для каждого известного source gap проследить request → acquisition/research outcome → actual read → claim decision **до** вызова автора на правку. Проверить, что обе роли действительно одобрили последний текст; два keep не подставлять программно. Если run остановлен или остались findings, это фактический результат, а не успешная приёмка. Пропущенные позднее проблемы оценивать по evidence/context, не запрещать их появление новым semantic gate.
