# 04. Проверка, допуск к выпуску и оценивание

Спецификация целевого продукта, 4 ноября 2026. Общие типы — [00](00-system-contract.md); источники — [01](01-sources-and-evidence.md); исследование — [02](02-research-and-editor.md); сценарий — [03](03-story-and-writing.md); звук — [05](05-audio-production.md); исполнение — [06](06-platform-and-runtime.md).

## 1. Ответственность

Подсистема проверяет конкретный результат: что утверждает готовый сценарий, подтверждают ли это материалы, понятна ли история и соответствует ли окончательный комплект проверенным версиям. Она не назначает автору число новых фактов на сцену и не заменяет редакционное решение универсальным score.

Разделяем три задачи: фактическая проверка сценария; редакционная оценка целого выпуска; оценивание качества самой системы на независимых случаях. Техническая проверка аудио принадлежит 05. Исполнение проверки без ошибок не означает положительного вердикта, а положительный вердикт одной модели не доказывает безошибочность продукта.

Авторские citations и ResearchNote служат указателями. Проверяющий читает сохранённые первоисточники, их окружение и существенные возражения. Отсутствие доказательства, доказанная ложность и честно обозначенная неопределённость — разные результаты. Все существенные ограничения должны быть слышны рядом с соответствующим выводом, а не спрятаны только в show notes.

## 2. Контракты артефактов

Ниже перечислены payload внутри ArtifactRevision. IDs, версии, producer, scope, input_refs и hash назначает backend. Ссылки — точные ArtifactRef; aliases допустимы только в model-facing вызовах и учебных примерах. Смысловые поля сохраняются полностью; previews — отдельные DerivedPreview.

### ClaimSet

```text
script_ref: ArtifactRef
claims: Claim[]
coverage: CoverageUnit[]
extraction_profile_ref: ArtifactRef
unresolved_extraction_questions: [{selector: ScriptSelector, question}]

Claim = {
  claim_id, statement, occurrences: ScriptSelector[],
  kind: factual | quantitative | causal | historical | quotation | attribution | prediction,
  qualifiers: {entities[], time_scope, geographic_scope, conditions[], modality, attribution|null},
  importance: central | supporting | incidental,
  candidate_evidence_refs: ArtifactRef[], related_claim_ids: UUID[]
}
CoverageUnit = {
  selector: ScriptSelector,
  classification: assertion | nonfactual,
  claim_ids: UUID[], exclusion_reason: string|null
}
```

ScriptSelector определён в 03: `{script_ref,selector:{act_id,block_id,item_id,char_start,char_end}}`; item_id разрешается в utterance/direction/placeholder ID, диапазон — Unicode code points `[start,end)`. Для coverage offsets заданы явно: `0 ≤ char_start < char_end ≤ len(text)`; char_start=0 допустим. Backend проверяет полное непересекающееся покрытие каждого utterance. Ссылки нескольких claims на одну фразу допустимы; они записываются в одном coverage interval. Stage directions/placeholders не превращаются в произнесённые факты. Вопрос с фактической предпосылкой тоже может содержать assertion.

Claim выделяется на уровне проверяемого утверждения с сохранёнными оговорками. «Число выросло, поэтому мера сработала» содержит изменение числа и причинный вывод; нельзя проверить первое и автоматически подтвердить второе. Короткая бытовая фраза не требует искусственного дробления на десятки объектов. Повтор объединяется только при одинаковых условиях, времени и модальности; все occurrences сохраняются.

claim_id устойчив в цепочке ClaimSet лишь при явном сопоставлении неизменившегося смысла и occurrences; mapping сохраняет backend. Сходство текста само по себе не переносит вердикт. importance не означает истинность или допустимость ошибки. Полное coverage доказывает обработку текста, но не семантическую полноту извлечения; она проверяется отдельным проходом ниже.

### VerificationReport

```text
script_ref: ArtifactRef
claim_set_ref: ArtifactRef
coverage_audit: {
  reviewer_run_ids: UUID[], reviewed_script_ranges: ScriptSelector[],
  findings: Finding[], unresolved_omissions: ScriptSelector[]
}
claim_reviews: ClaimReview[]
cross_claim_findings: Finding[]
source_family_assessment_refs: ArtifactRef[]
calculation_refs: ArtifactRef[]
policy_ref: ArtifactRef
recommendation: accepted | changes_required | rejected
remaining_work: [{finding_id, proposed_action, research_request_ref|null}]

ClaimReview = {
  claim_id,
  verdict: supported | supported_with_limits | contradicted | insufficient | not_verifiable,
  evidence_reviews: [{evidence_ref, relation: supports|qualifies|contradicts,
                     source_applicability, original_inspection, rationale}],
  counterevidence_refs: ArtifactRef[], calculation_refs: ArtifactRef[],
  family_assessment_refs: ArtifactRef[],
  stated_limits: string[], audible_limit_selectors: ScriptSelector[],
  findings: Finding[], rationale, reused_from_report_ref: ArtifactRef|null
}
Finding = {
  finding_id, category, location_selectors: object[], explanation,
  basis_refs: ArtifactRef[], severity: critical|major|minor|advisory,
  blocking: boolean, policy_rule_id, required_outcome
}
```

Все claims получают результат, включая пока непроверенные. insufficient означает недостаток опоры, not_verifiable — невозможность проверить заявленное в доступных границах, а не разрешение считать его истинным. Предсказание проверяется как корректно приписанное предсказание с предпосылками; будущий исход не объявляется подтверждённым. Чужое высказывание и истинность его содержания — отдельные claims, когда сценарий утверждает оба.

supported_with_limits допускается лишь если конкретные ограничения уже выражены в проверяемом тексте и смысл всей реплики им соответствует. Фраза «это лишь гипотеза» не исправляет следующую безусловную причинную историю. Quote-проверка включает точность, говорящего, контекст, перевод и пропуски. Числовая — единицы, знаменатель, период, округление и источник входов. Causal-проверка отдельно рассматривает механизм, альтернативы и то, что метод источника действительно позволяет заключить.

source_applicability хранит время/условия/компетенцию/метод, а не рейтинг сайта. SourceFamilyAssessment из 01 версионируется и относится к конкретному выводу/входным данным; неизвестная зависимость не считается независимостью. Два пересказа одной таблицы не являются двумя независимыми измерениями. original_inspection содержит показанные page/time/text ranges и ReadReceipt IDs; receipt доказывает передачу, не понимание.

Coverage audit и cross-claim review обнаруживают пропуски, потерю отрицаний/оговорок, неверное объединение claims, причинные переходы между репликами и общее впечатление, противоречащее источникам. Общая factual precision не даёт допуска при одной ошибке, меняющей центральный ответ.

### EditorialReview

```text
episode_spec_ref: ArtifactRef
story_plan_ref: ArtifactRef
script_ref: ArtifactRef
listener_state_ref: ArtifactRef
policy_ref: ArtifactRef
first_read_ref: RuntimeRef                    # finalized ToolExecution(record_first_read)
spoken_view_hash: string
dimensions: [{dimension, assessment, evidence_selectors: ScriptSelector[], rationale}]
findings: Finding[]
listener_answer: {main_answer, causal_chain[], important_limits[], unanswered_promises[]}
revision_comparison: {prior_script_ref, target_finding_ids[],
  repaired[], preserved_strengths[], regressions[], preferred_action}|null
recommendation: accepted | changes_required | rejected
```

Обязательные dimensions: выполнение обещания; развитие понимания; понятность механизмов; уместность примеров; темп и повторы; связность переходов; завершение; естественность/честность ведущих; соответствие аудитории и длительности. assessment=works|needs_revision|not_applicable с конкретным основанием; средний балл не компенсирует провал центрального объяснения. ListenerState — подсказка, reviewer самостоятельно читает сценарий.

Первый читатель получает `spoken_view(script_ref)` из 03. В порядке произнесения видны только ID реплик, спикеры и текст. Служебные данные связывают вид с точной версией сценария и диапазонами символов. В `EditorialReview.spoken_view_hash` сохраняется `spoken_view.view_hash` из 03. Контекст первого чтения закрыт от заголовков актов, ремарок, author bindings, плана, досье, ListenerState, экспертных ответов и прежних оценок. Эта граница действует и для доступных инструментов.

Длинный выпуск и спорные переходы читатель проходит последовательно. Компилятор ограничивает контекст и инструменты уже выданным префиксом. На границе окна `record_first_read(mode=prefix)` сохраняет понятое, открытые вопросы, сильные места и первый замеченный разрыв с точными репликами. Даже отсутствие разрыва фиксируется явно. Backend сверяет последнюю выданную реплику с фактическими диапазонами и финализирует ToolExecution. Только после этого следующий manifest открывает очередное окно. Исходные наблюдения остаются неизменными. Финальная запись проверяет непрерывное покрытие всего spoken_view, включая середину и стыки. Пропущенное окно требует дочитывания, исчерпанный предел задания даёт partial.

До раскрытия других материалов читатель вызывает `record_first_read(mode=final)`. Он сохраняет понятый вопрос и ответ своими словами, ссылки на наблюдения префиксов и вывод после дочитывания. Если позднее объяснение закрывает вопрос, отдельная запись follow-up связывает исходный RuntimeRef и реплику разрешения. Так можно отличить работающую отсрочку от разрыва, который испортил следующий вывод.

```text
FirstBreak = {observation_mode: prefix|retrospective, through: ScriptSelector,
  at: ScriptSelector, last_clear_at: ScriptSelector|null,
  understood_so_far: string, missing_link: string}
BreakFollowUp = {prefix_observation_ref: RuntimeRef,
  resolution: explained_later|unresolved, resolution_at: ScriptSelector|null,
  downstream_effect: string, scope: utterance|scene|act|episode}
```

Если весь текст показан одним вызовом, найденный задним числом FirstBreak получает observation_mode=retrospective. При споре отдельный читатель получает только префикс до выбранной границы, без будущих реплик, прежнего вывода и подсказки, что именно искать. Его `record_first_read(mode=prefix)` даёт независимую проверку, которую редактор связывает с ретроспективным наблюдением. Если читатель достраивает механизм из собственных знаний, он указывает внешнее предположение и место в тексте. В обоих режимах модель формулирует гипотезу о восприятии. Её проверяют по ответам реальных слушателей в evaluation.

Backend добавляет script_ref, spoken_view_hash, доставленные ranges и IDs invocation. В EditorialReview.first_read_ref сохраняется RuntimeRef финализированного **итогового** ToolExecution06. Он связывает записи префиксов и отдельные разрешения. Этот RuntimeRef включается в manifest следующей фазы. Только затем редактор получает EpisodeSpec, план, ListenerState и необходимые материалы, сопоставляет замысел с первым чтением и принимает решение. Каждая новая ScriptRevision получает новый первый проход без доступа к старому review.

Формальная проверка устанавливает совпадение версии, hash, порядок фаз и покрытие реплик. Смысловая проверка устанавливает, объяснена ли связь, где именно слушатель её теряет и какого размера правка нужна. Для диалога рецензент показывает конкретный обмен, где второй голос меняет понимание, ритм, эмоциональный эффект или направление разговора. Пробное удаление дублирующей реплики помогает проверить её функцию. Смена спикера или количество новых фактов сами по себе функцию не устанавливают.

listener_answer — модельная реконструкция того, что можно понять из текста, не измерение реального слушателя. В benchmark это проверяют отдельные люди. Ведущие не изображают личный опыт, интервью или наблюдение, которых не было; условная сцена/аналогия должна распознаваться как условная. Драматургия не даёт права придумывать факт или создавать ложное равновесие версий.

### CalculationRecord

```text
purpose: string
inputs: [{name, value_decimal, unit, evidence_refs: ArtifactRef[], source_selector}]
expression: RestrictedExpression
execution_profile_ref: ArtifactRef
rounding: {precision, mode, display_scale}
result: {value_decimal, unit, display_text}|null
status: computed | failed
error: {code, message}|null
limitations: string[]
```

RestrictedExpression — типизированное AST с decimal literals, именованными входами, add/sub/mul/div, проверенными агрегатами и явными unit conversions из versioned registry. Нет Python eval, imports, file/network access, shell, SQL или произвольных функций. Более сложный метод реализуется как заранее проверенная versioned calculation module с открываемым описанием; неизвестная операция возвращает unsupported_calculation, а не код, написанный источником.

Начальная числовая реализация — decimal arithmetic с закреплёнными precision/rounding/traps; division-by-zero, invalid/nonfinite и несовместимые единицы дают failed. Decimal устраняет часть ошибок представления десятичных чисел, но не отменяет округление: правила обязаны быть явными. [Python decimal](https://docs.python.org/3/library/decimal.html). Exact values сериализуются строками согласно 06. Output не становится EvidenceSpan: это воспроизводимое вычисление над evidence. Проверяющий отдельно оценивает правильность выбранной формулы и исходных величин.

## 3. Алгоритм проверки текста

1. Закрепить production ScriptRevision и EpisodeSpec/StoryPlan/DossierSnapshot/policy. Backend проверяет selectors, completeness, отсутствие research_placeholder и недоступных refs. Черновики можно проверять раньше, но отчёт по fragment не даёт допуска полному выпуску.
2. Извлечь claims из spoken_view этой версии. После первичного извлечения открыть авторские citations как кандидаты для проверки. Утверждение без авторской ссылки проверяется наравне с остальными.
3. Независимый invocation читает полный script и ищет проверяемые assertions/предпосылки/связки, ещё не ориентируясь на готовые классификации. Затем сопоставляет с ClaimSet, предлагает omissions/overbundling/qualifier loss. Backend проверяет coverage. При исправлении создаётся новый ClaimSet; финальный coverage audit относится именно к нему.
4. Для каждого claim проверить кандидатную опору через01: полный relevant context, original при OCR/ASR/переводе/спорной интерпретации, даты и область применимости. Дополнительно искать сильные возражения к центральным и спорным выводам; scope не расширяется молча.
5. Проверить вычисления через CalculationRecord. Проверить quoted words отдельно от вывода, а временной порядок — отдельно от причинности. Источник, который сам лишь предполагает причину, не подтверждает безусловную формулировку сценария.
6. Проверить связи между claims и существенный итог: не построена ли ложная история из по отдельности верных фраз, не исчезли ли ограничения при переходе между ведущими.
7. Сохранить VerificationReport с полным результатом/нерешённым. Параллельно или отдельно выполнить EditorialReview целого script. Проверяющие не редактируют принятый сценарий непосредственно.
8. Editor принимает адресные исправления/ResearchRequest либо меняет историю. Новая ScriptRevision проходит extraction/coverage/recheck и повторный global editorial review. Только затем начинается озвучивание по05.

Claim extraction применяется к готовому тексту как инструмент проверки, а не как предварительная модель всего знания. FActScore показывает полезность проверки отдельных фактических утверждений, но его исходная оценка на биографиях не подтверждает качество причинной истории или полноту этого pipeline. [FActScore](https://aclanthology.org/2023.emnlp-main.741/). Различия между типами задач требуют собственных случаев продукта, а не переноса одного benchmark score. [VeriScore](https://aclanthology.org/2024.findings-emnlp.552/).

### Независимость и ограничения автоматического review

Независимость означает отдельную роль/invocation с собственной постановкой и доступом к первоисточникам, а не достаточность другого названия агента. Разные вызовы одной модели могут воспроизводить одну ошибку; две модели тоже не гарантируют независимости. Не передаём положительный вердикт автора как исходную истину. Разные model families проверяются как эксперимент, не вводятся ритуально.

Runtime сохраняет model-visible context, tools, read receipts, report и короткие основания. Не запрашиваются скрытые рассуждения. Reviewer не может снять блокировку потому, что «история звучит убедительно». При неясном выводе он сохраняет disagreement/needs_material; editor выбирает конкретное доисследование или изменение формулировки.

## 4. Контекст и инструменты

Factchecker получает вопрос/аудиторию/as_of, exact script и локальные соседние реплики, claims/qualifiers, candidate evidence, source policy и критерии вердиктов. Для editorial review компилятор сначала открывает только разрешённый префикс spoken_view. После финализированного `record_first_read(mode=final)` он открывает план и остальные разрешённые входы. При переполнении окна возвращается context_capacity_exceeded и меняется организация чтения, с сохранением полного текста.

| Tool | Model-facing аргументы | Результат/действие |
|---|---|---|
| read_script | script_alias, locator?, context, cursor? — контракт03 | Точный текст и соседний контекст с alias/offsets |
| record_first_read | mode=prefix\|final, reconstructed_answer, open_promises, strengths, first_break либо null, supporting_utterance_aliases; для final также prefix_refs, follow_ups, final_observation | Prefix фиксирует реакцию до следующего окна, final связывает итог с предыдущими наблюдениями. Возвращает RuntimeRef финализированного tool_execution |
| search_sources, read_source, inspect_evidence, compare_sources | Контракты01 | Поиск/чтение оригинала, provenance и точные EvidenceSpan |
| run_calculation | purpose, named_inputs, expression, rounding | CalculationRecord alias либо typed failure |
| save_claim_set | claims, coverage, unresolved_questions | Проверенные selectors и новая версия ClaimSet |
| save_verification_report | claim_set_alias, reviews, coverage_audit, cross_claim_findings, remaining_work | Immutable report; никакого автоматического исправления script |
| save_editorial_review | dimensions, findings, listener_answer, revision_comparison?, recommendation | Backend связывает review с финализированным первым чтением, проверяет refs и фазу |
| request_research | requester_alias, location?, question, why, current_understanding, acceptance_questions, impact, blocking, requested_outcome | ResearchRequest02; location — типизированный selector владельца script/plan/report, backend добавляет context_refs из выбранных aliases |
| finish_review | status, report_aliases, remaining_questions, next_action | Стандартный TaskResult00; success не approval |

Backend добавляет IDs, input refs, policy и producer; response/error — ToolResult06. Неизвестный alias, неверный диапазон, неполное coverage, отсутствие claim review или recommendation=accepted при blocking findings дают validation_error с адресом поля. Эти структурные проверки не выносят семантический verdict вместо модели.

Model-facing selections выбираются по показанным fragments; backend вычисляет offsets и точные ScriptSelector. request_research не встраивается в тот же report, если это создаёт взаимные ссылки: сначала сохраняется report R1, затем request с requester_ref=R1, затем новая revision/report при необходимости. Source policy/ACL проверяются повторно при каждом tool call. Источник и script могут содержать вредоносные инструкции; для reviewer это данные, а не полномочия.

```text
FACTCHECKER
Проверь конкретные утверждения этого текста в пределах времени и условий.
Не доверяй авторским ссылкам без чтения. Различай утверждение источника,
его интерпретацию и факт высказывания. Ищи потерянные ограничения/возражения.
Для каждого вердикта сохрани опору и краткое основание. Если материала
не хватает, укажи что именно нужно; не достраивай источник по памяти.
Проверь также связи между репликами и пропущенные assertions.
```

```text
EDITORIAL REVIEWER
Прочитай выпуск как слушатель заданной аудитории. Сначала восстанови
ответ и путь объяснения только из текста. Покажи конкретные места,
где понимание/интерес теряются или обещание не выполнено.
Полезный callback не равен плохому повтору. Новая ссылка не равна новой мысли.
Предлагай нужный результат исправления, а не переписывай всё своим стилем.
После изменения сценария оценка должна быть выполнена заново.
```

## 5. Исправления, версии и условия допуска

Каждый finding задаёт наблюдаемый результат исправления. Editor выбирает доисследование, уточнение, перестройку или удаление неверного. Автор возвращает новую ScriptRevision и ScriptChangeSet03. Проверяющий перечитывает изменённые места вместе с соседними репликами. Новый global EditorialReview обязателен после изменения сценария, включая перестановку актов.

После независимого первого чтения новой версии проводится сравнение с предыдущей. В revision_comparison проверяются устранение исходного дефекта и сохранность сильных мест, отмеченных при первом чтении предыдущего текста. Рецензент указывает точные реплики обеих версий, объясняет, что стало понятнее и что потеряло функцию. Если при устранении неверной даты перепись разрушила работающий финальный возврат, проверяется вариант с точечным исправлением даты. Editor выбирает лучший из допустимых вариантов или возвращает текст на переработку. Для полностью корректного оригинала допустимо решение сохранить его. Фактический blocker нельзя снять предпочтением слушателей.

ClaimSet пересобирается на новой revision. Условия reuse отдельного ClaimReview: совпадают statement/qualifiers, утверждение в новом окружении не приобрело иной смысл, evidence/revision/date applicability/policy не изменились, original inspection остаётся применимым и нет новых противоречий. Новый report явно хранит reused_from_report_ref и результат проверки условий. Совпавший hash текста или прежний accepted не разрешает автоматически перенести весь допуск.

История происхождения и текущая содержательная зависимость различаются. `parent_script_ref` и ссылка на прежний report сохраняют lineage, но не заставляют исправленную версию вечно наследовать ошибку предка. В VerificationReport добавляется:

```text
dependency_dispositions: [{
  prior_input_ref: ArtifactRef, current_input_refs: ArtifactRef[],
  affected_selectors: ScriptSelector[],
  disposition: retained | replaced | removed | lineage_only,
  basis, checked_against_script_ref: ArtifactRef, reviewer_run_id: UUID
}]
```

Disposition оценивает новый текст и точные входы. retained сохраняет действующую содержательную зависимость; replaced указывает одну или несколько новых опор; removed требует проверки отсутствия зависимого утверждения во всём сценарии, включая перефразирование; lineage_only означает только исторический/прочитанный контекст, который не служит основанием текущего утверждения. Пустой current_input_refs допустим для removed/lineage_only. Удаление из script не снимает зависимости активного dossier/ShowNotes: их владельцы проверяют свои точные версии отдельно.

Для ScriptRevision только этот exact VerificationReport вместе с действующим factual AcceptanceRecord разрешает revalidation. Это частный случай общего DependencyRevalidationEvent00/06: adapter задаёт subject_ref=script_ref, assessment_ref=report ref, authority с ID принятия и переводит affected_selectors в канонические subject_selectors после проверки checked_against_script_ref. Для ShowNotes основанием служат ShowNotesReview и show_notes AcceptanceRecord; для notes/audio действуют assessment/authority их владельцев00. Script report не снимает зависимости чужого subject.

Event append-only и CAS по expected_validity_version меняют только текущую eligibility проверенного subject: старые refs/trace не стираются. Lineage не распространяет редакционный invalidation. Пока disposition не проверен, действует консервативная зависимость от фактически показанных материалов. Ограничения доступа и удаление копий данных06 не снимаются редакционным disposition.

Severity описывает последствия: critical — меняет центральный ответ/серьёзно вводит в заблуждение; major — существенная локальная ошибка или провал объяснения; minor — ограниченный дефект; advisory — необязательное улучшение. `blocking` определяется rule policy, а не усреднением severity. Начальная политика:

| Состояние | Допуск |
|---|---|
| Пропущенные assertions, непроверенный claim, insufficient/not_verifiable assertion о мире | Блокировать до проверки, уточнения или удаления; «мелкий факт» не освобождает от исправления |
| Contradicted claim, выдуманная цитата/опыт, неверное число/причинность/attribution | Блокировать независимо от общей factual precision |
| Supported_with_limits, ограничения явно слышны и не опровергнуты общей историей | Допустимо с проверенными audible selectors |
| Центральное обещание не выполнено, механизм непонятен, финал противоречит рассказу | Блокировать по EditorialReview |
| Вкусовая альтернатива перехода/необязательный stylistic polish | Advisory; не запускать бесконечную перепись ради предпочтения reviewer |
| Истёк лимит, недоступна модель/опора, review неполон, остался placeholder | Сохранить partial/failed; не допускать выпуск |
| В текущей ветке открыт blocking ResearchRequest со значением passage или central_answer | Блокировать текстовый допуск, TTS и сборку до принятого редактором решения |
| Неверная версия report/audio/transcript, отозванный input, неподтверждённый audio QA | Блокировать сборку комплекта |

Законная неопределённость — содержательный ответ с проверяемой формулировкой «данные не позволяют различить эти причины», а не unsupported утверждение плюс общий disclaimer. Умеренный тон тоже не превращает предположение в доказанный факт.

Review предлагает recommendation; application service проверяет полномочия reviewer, согласованность findings и точные refs, затем записывает AcceptanceRecord00. Для текста subject_ref=ScriptRevision: отдельно check_kind=factual с выбранным VerificationReport и check_kind=editorial с EditorialReview в reviewed_input_refs. Для звука check_kind=audio, subject_ref=AudioMaster с AudioQAReport/Transcript/PerformancePlan05. Для публикационного текста check_kind=show_notes, subject_ref=ShowNotes с ShowNotesReview. У этих проверок assessment_scope=null; qualification использует check_kind=profile_qualification и только QualificationScope00 с явными supported_cells. Принятие связано с policy/квалификацией и может быть отозвано append-only; наличие файла report не равнозначно допуску.

Перед текстовым допуском сервис разрешает активные ResearchRequest по авторитетной проекции текущей ветки 02/06. Проверка по placeholders, open_research_request_refs одного сценария или списку findings недостаточна. Удаление реплики закрывает passage/central_answer только после принятого редактором обоснования с привязкой к новой версии. Запрос с blocking=none позволяет продолжить производство. Новое блокирующее событие меняет validity и запускает повторную проверку перед TTS05 и сборкой комплекта.

Начальный `max_review_revision_cycles=3` — операционный предел для калибровки, не допустимое число ошибок. Повтор одной проблемы вызывает диагностику prompt/payload/tools/trace и адресное изменение задания. По исчерпании сохраняются drafts/findings и честный needs_attention; качество не объявляется достигнутым. Новая содержательная стратегия editor имеет новое задание и записанное основание, а не бесконечное повторение той же попытки. Человек не утверждает каждую реплику: он нужен для разработки критериев и исключительного изменения пользовательского запроса.

## 6. ShowNotes и точный комплект выпуска

ShowNotes создаются после окончательного AudioMaster/Transcript. Заголовок, описание и названия глав могут внести новый ложный факт, даже если сценарий проверен; их нельзя исключать из контроля как «метаданные».

```text
ShowNotes.payload = {
  script_ref, transcript_ref, audio_master_ref,
  title, description,
  chapters: [{chapter_id, title, summary, start_frame, script_selectors[]}],
  citations: [{citation_id, source_revision_ref, evidence_refs[],
              display_metadata, public_locator|null, supported_script_selectors[]}],
  disclosures: [{kind, text}], uncertainty_notes: [{text, script_selectors[]}]
}
ShowNotesSelector = {show_notes_ref, field, item_id|null, char_start, char_end}
ShowNotesReview.payload = {
  show_notes_ref, script_ref, verification_report_ref, policy_ref,
  reviewed_fields: ShowNotesSelector[],
  content_checks: [{statement, selectors: ShowNotesSelector[],
                   evidence_refs[], calculation_refs[], verdict, rationale}],
  dependency_dispositions: DependencyDisposition[],
  findings: Finding[], recommendation: accepted|changes_required|rejected
}
```

`field` — title|description|chapter_title|chapter_summary|citation_metadata|disclosure|uncertainty_note. Диапазоны — code points; chapter/citation identity задаёт item_id. title/description/все главы проверяются полностью, в том числе рекламное преувеличение причинности и итогов. `verdict` использует enum ClaimReview. DependencyDisposition берётся из00, subject_selectors указывают только на exact ShowNotes этого review. Необходимая новая опора получает обычный ResearchRequest; дописывать факт только в description без проверки нельзя.

start_frame относится к canonical PCM master timeline из AudioMaster05; sample_rate_hz/sample_frames берутся из того же artifact. Для playback/export применяется его проверенный playback_decode_map, без предположения нулевого encoder delay. Chapters должны ссылаться на реальную границу соответствующего материала, не на расчёт числа слов. Проверяются порядок/границы, mapping в Transcript, точность bibliographic metadata и допустимость публичного раскрытия источников. Полные закрытые материалы и private locators не экспортируются. ShowNotes не ссылаются на собственный review, поэтому цикл отсутствует.

ReleaseManifest берётся целиком из00: обязательны purpose, evaluation_case_ref (nullable только для production), performance_plan_ref и show_notes_review_ref наряду с остальными exact refs. Перед production ready assembler06 атомарно проверяет:

1. `StoryPlan.dossier_ref = ScriptRevision.dossier_ref = ReleaseManifest.dossier_ref` и `StoryPlan.episode_spec_ref = ScriptRevision.episode_spec_ref = ReleaseManifest.episode_spec_ref`. Артефакты относятся к выбранной ветке и head, script production+complete, без placeholders. В актуальной проекции 02/06 отсутствуют открытые blocking passage/central_answer. Проверка проекции и commit должны видеть одну validity version.
2. VerificationReport и EditorialReview проверяют этот script. ClaimSet относится к нему. ListenerState итогового review покрывает **весь** spoken_view этого script с совпадающими порядком, границами и hash. Действуют отдельные factual/editorial AcceptanceRecord с соответствующими reviewed_input_refs при текущей policy и разрешённых входах.
3. Manifest.performance_plan_ref совпадает с refs AudioMaster/Transcript/master-scope AudioQAReport; PerformancePlan ссылается на этот script и выбранные text reports. Master/transcript/QA имеют одинаковый audio_master_ref и script_ref; действует audio AcceptanceRecord. Segment QA не заменяет master QA; измеренная длительность соответствует политике EpisodeSpec.
4. ShowNotes ссылаются на эти audio/transcript/script; ShowNotesReview и его acceptance проверяют именно их. Нет непроверенного нового factual content.
5. Все обязательные refs доступны, active dependencies не отозваны, current source/freshness policy соблюдена; generation epoch/head/validity version не изменились во время сборки. Для purpose=production evaluation_case_ref=null; exact profiles квалифицированы для выбранной supported cell00.

Assembler сохраняет immutable manifest и eligibility record; ready означает готовность конкретного комплекта, не внешнюю публикацию. Любая правка создаёт новую цепочку artifacts/допусков. Старый manifest остаётся исторической записью, а текущая пригодность вычисляется отдельно.

Qualification experiments выполняются с EpisodeRun/TaskRun.purpose=evaluation и точным EvaluationCase, разрешающим candidate profiles. Тот же assembler проверяет полноту, версии и содержательные допуски; единственное исключение — отсутствие production qualification у явно разрешённого candidate. Успех даёт внутренний ReleaseManifest(purpose=evaluation,evaluation_case_ref=case), доступный evaluator-only playback. Пользовательский ready/export запрещён. Провал остаётся failed/inconclusive EvaluationResult с outputs, без успешного manifest; эксперимент не получает обход factual/audio checks.

Для проверки слабых и намеренно испорченных текстов у evaluator есть отдельная **нейтральная исследовательская читка**. Разрешённый EvaluationCase задаёт закрытый корпус и одинаковую подачу вариантов. Evaluator читает exact spoken_view человеком или фиксированным голосовым профилем, проверяет сохранность реплик, закрепляет script_ref, spoken_view_hash, условия записи, checksum и фактическое число frames в финализированном ToolExecution06. У материала только evaluator ACL. Он доступен для слепого задания даже когда исследуемый ScriptRevision не получил factual/editorial допуск. Он не создаёт PerformancePlan, production AudioMaster, AcceptanceRecord или пользовательский выпуск. Обычный выпуск проходит все проверки 03–06.

DAG: Script → ClaimSet → VerificationReport; Script/StoryPlan/ListenerState → EditorialReview; Script → PerformancePlan → AudioMaster → Transcript → AudioQAReport; AudioMaster/Transcript/Script → ShowNotes → ShowNotesReview; выбранные артефакты → ReleaseManifest. Стрелки здесь означают «используется для создания», а не embedding дочернего ref в родительский payload.

## 7. Примеры и отказоустойчивость

Учебный corpus о вымышленном городском транспорте из01: протокол сообщает 12 недель пилота, таблица — 3200 поездок до изменения и 4000 после. Эти данные не устанавливают причину изменения спроса.

Плохой сценарий: «За 12 недель новый график увеличил число поездок на 25 процентов». Extractor должен сохранить минимум три проверки: срок, относительное изменение, причинность. CalculationRecord вычисляет `(4000−3200)/3200×100=25`; это поддерживает арифметику, но причинный claim остаётся insufficient. Если extractor извлёк только число, независимый coverage audit обнаруживает пропущенную связь.

Исправление: «В протоколе сравниваются 3200 и 4000 поездок — рост на четверть. Но из этой таблицы нельзя понять, вызван ли он новым графиком». Новая revision получает собственные claims/report/editorial review. При наличии источников о других факторах reviewer требует соответствующее уточнение; слово «нельзя» тоже проверяется по методике/границам этих данных, а не объявляется фактом о всей науке.

Диагностический фрагмент результата, не полный persistence envelope:

```json
{
  "claim_id": "c-causal",
  "verdict": "insufficient",
  "rationale": "Таблица сравнивает периоды, но не отделяет эффект графика от других факторов.",
  "findings": [{
    "category": "causal_overstatement", "severity": "major", "blocking": true,
    "policy_rule_id": "factual_unresolved",
    "required_outcome": "Найти причинную оценку или убрать утверждение о доказанном эффекте."
  }]
}
```

Другой допустимый случай: «Один отчёт называет причиной X, другой — Y; их методы не позволяют снять разногласие». Подтверждаются attribution, содержание разногласия и пределы методов. Недопустимый вариант — два домена с одним первоисточником, превращённые в «два независимых исследования», либо искусственное равновесие отозванной и воспроизводимой работы.

| Сбой/конфликт | Восстановление |
|---|---|
| Claim extraction оборвался на середине | Сохранить diagnostic/checkpoint, честный partial/failed по00; новый task продолжает с boundary context. Final ClaimSet публикуется после полной структурной сверки |
| Источник недоступен | needs_material/research request; нет автоматического supported по cached summary |
| Новый источник исправляет число | Invalidation, новый report/при необходимости script; current approval отозван до решения |
| Автор изменил текст во время review | Report остаётся для старого script; новый TaskSpec/контекст, stale результат не принимается на новый head |
| Reviewer выдал противоречивые verdict/findings | Typed validation_error или отдельная adjudication task с оригиналами; не majority vote без разбора |
| Повторная доставка save_report | Тот же operation ID возвращает прежний artifact; нет второго acceptance |
| Аудио пропустило отрицание |05 блокирует master; пересинтез/QA. Если меняют сам текст — возврат03/04 |
| Источник удалён из private project | Refs честно unavailable; purge/retention06 распространяются на reports/trace/copies, review не обходится |

## 8. Оценивание продукта и компонентов

### Данные и воспроизводимость

EvaluationCase, EvaluationResult, EvaluationAnnotation и HumanAssessment — артефакты00. Generation workers не имеют доступа к скрытым ответам, экспертным annotations или judge verdicts. Case split делается по темам/связанным источникам, а не по перефразировкам одного запроса.

```text
EvaluationCase.payload = {
  case_family, split: development|calibration|holdout, topic_group_id,
  mode: component|episode|failure_recovery,
  episode_spec_ref, input_refs[], source_access_policy_ref,
  reference_annotation_ref, scoring_policy_ref, allowed_candidate_profile_refs[],
  supported_combination: {language, duration_seconds, format},
  perturbations[], prohibited_shortcuts[]
}
EvaluationResult.payload = {
  case_ref, candidate_profile_refs[], repeat_index,
  output_refs[], run_ids: UUID[], evaluator_profile_refs[],
  measurements[], human_assessment_refs[], findings[],
  outcome: pass|fail|inconclusive, exclusions_with_reasons[],
  started_at, completed_at
}
```

reference_annotation_ref указывает на следующий типизированный artifact; это ожидаемые свойства ответа, а не единственный разрешённый текст:

```text
EvaluationAnnotation.payload = {
  episode_spec_ref: ArtifactRef, source_snapshot_refs: ArtifactRef[],
  source_assertions: [{annotation_id: UUID, statement: string,
    evidence_refs: ArtifactRef[], required_qualifiers: string[],
    importance: central|supporting|incidental}],
  expected_understanding: [{question: string, answer_criteria: string[],
    acceptable_alternatives: string[], basis_refs: ArtifactRef[]}],
  forbidden_inferences: [{statement: string, reason: string, basis_refs: ArtifactRef[]}],
  known_uncertainties: [{description: string, basis_refs: ArtifactRef[]}],
  scoring_policy_ref: ArtifactRef, annotator_assignment_ids: UUID[]
}
HumanAssessment.payload = {
  case_ref: ArtifactRef, evaluated_output_refs: ArtifactRef[], rubric_ref: ArtifactRef,
  assignment: {assignment_id: UUID, anonymous_rater_id: string,
    role: factchecker|editor|listener, blind_candidate_ids: string[],
    presentation_order: string[], task: source_audit|editorial_review|
      full_comprehension|voluntary_interest|pairwise_preference},
  ratings: [{dimension: string, score: integer|null, abstain_reason: string|null,
    rationale: string, evidence_selectors: SubjectSelector[]}],
  fact_checks: [{statement: string, location: SubjectSelector,
    verdict: ClaimReview.verdict, evidence_refs: ArtifactRef[], rationale: string}],
  comprehension: {free_answer: string, stated_limits: string[], transfer_answer: string}|null,
  audio_coverage: {subject: {kind: master, audio_master_ref: ArtifactRef}
      | {kind: evaluation_reading, recording_ref: RuntimeRef, script_ref: ArtifactRef,
         spoken_view_hash: string}, sample_rate_hz: integer, sample_frames: integer,
    listened_ranges: [{start_frame: integer, end_frame: integer}],
    completion: full|partial, omitted_ranges_with_reasons: [{start_frame,end_frame,reason}]}|null,
  voluntary_observation: {outcome: not_started|completed|stopped_content|
      stopped_external|technical_failure|observation_end|lost_followup|unknown_reason,
    last_frame: integer|null,
    location: ScriptSelector|null, stated_reason: string|null,
    prior_topic_interest: string|null, prior_familiarity: string|null}|null,
  pairwise_preference: {winner_blind_id: string|null, outcome: preferred|tie|abstain,
    rationale: string}|null,
  completed_at: Timestamp
}
```

ratings.score использует anchors0–4 ниже. Для null нужен abstain_reason. SubjectSelector задаётся в 00. Время относится к frames точного master05 либо закреплённой исследовательской читки. Для full_comprehension требуется непрерывное покрытие всей записи с совпадающими refs. Понимание проверяется по пересказу и ответам. Неполная попытка этой когорты сохраняется отдельно. Для voluntary_interest остановка — наблюдаемый исход задания, который остаётся в выборке. UI скрывает identities моделей, провайдеров и авторские оценки до завершения оценки.

Annotation создаётся из spec/источников до EvaluationCase и не ссылается обратно на будущий case. HumanAssessment ссылается на case и оцениваемые outputs, никогда на будущий EvaluationResult; Result собирает refs уже завершённых assessments. Исправленная оценка — новая revision. Все annotations/ratings и их previews имеют evaluator-only ACL; generation context получает только разрешённые spec/inputs, не полный EvaluationCase с закрытой разметкой. Поиск, refs и trace не обходят этот запрет. Приватные персональные данные оценщиков в runtime trace не нужны.

Начальный набор: 36 тем — по 12 development/calibration/holdout, с отдельными source families между splits. Это стартовая схема, не доказательство статистической достаточности. В каждом split нужны: документированная процедура; причинное объяснение; историческая реконструкция; конфликт источников; количественная/табличная тема; тема с обоснованной неопределённостью. Включить мультиязычные материалы, OCR/ASR, числовые совпадения, перепечатки, удалённую опору и prompt injection. Гонконгская валюта может быть одним regression case, но не единственным предметом настройки.

Квалификация выполняется по support matrix язык×длительность×format×voice/model profiles. Начальный целевой набор длительностей:15/30/60/90 минут; языки: русский/английский; solo/dialogue. Для каждой объявляемой поддержанной комбинации нужны полные end-to-end cases, включая трудные сценарии. Прохождение короткого английского solo не квалифицирует 90-минутный русский диалог. Непроверенные комбинации не объявляются поддержанными; это не ограничение целевой архитектуры.

### Контролируемые сравнения

| Эксперимент | Что изолирует |
|---|---|
| Простая сильная модель с теми же источниками/tools против редакционного процесса | Пользу организации работы, не различие доступных знаний |
| Обоим авторам одинаковое экспертное досье | Способность построить рассказ без ошибки исследования |
| Экспертный план + одинаковое досье против самостоятельно выбранной истории | Потери выбора структуры и передачи понимания |
| Полный script одним проходом против актов с global context | Качество длинной связности; не только формат output |
| Один scoped вопрос с разными context/tool contracts | Причину зависания/потери смысла при фиксированной модели |
| Один контракт с разными ModelProfile | Возможности модели при неизменной задаче |
| Известно испорченные script/audio/evidence versions | Recall проверяющих, false passes и восстановления |
| Тот же script с разными TTS/performance profiles | Качество звучания отдельно от качества текста |

Baseline и кандидат получают одинаковый source access, тему, дату, язык, длительность и независимую итоговую проверку. Экспертное досье — отдельный oracle experiment, не привилегия одной стороны end-to-end сравнения. Для repeat фиксируются snapshots/configs, но новый вызов модели реально выполняется: application cache отключён либо repeat помечен cached и не считается независимым запуском. Live-web испытания отдельны от frozen corpus; изменение поиска не маскируется улучшением модели.

В EXP-04 единица сравнения письма — пара с одинаковыми EpisodeSpec, StoryPlan, DossierSnapshot, writer ModelProfile, разрешёнными инструментами и правилами редакторских возвратов. Зафиксировать первые полные черновики до редакционных исправлений, а затем версии после одинаковой процедуры review. Оба набора предъявляются вслепую. Для слушательской части используются одинаковые голоса и условия читки. Если whole-episode не помещается в output window, зарегистрировать отказ по выполнимости. Литературное сравнение вести отдельно среди завершённых пар, а продуктовый итог — по всем назначенным попыткам, включая провалы. Влияние собственного выбора StoryPlan исследуется отдельным сравнением.

Порядок EXP-04 задан в 07. Прямое письмо, прямое письмо с дополнительной авторской правкой и переработка связного черновика в диалог сравниваются на одних входах. Дополнительный авторский проход отделяет эффект второй попытки от эффекта смены формы текста. Профессиональные автор и редактор получают тот же бриф, досье и план. Их закрытые черновик и финал проходят ту же слепую оценку. Варианты одной темы предъявляются разным слушателям при измерении понимания.

Начально три независимых генерации каждой сравниваемой ячейки, затем размер расширяется по разбросу и требуемой точности решения. Три — диагностика вариативности, не гарантия мощности. Вместо выбора лучшего из трёх сохраняются все попытки, включая failed/needs_attention. Один успешный demo не квалифицирует конфигурацию.

### Что измерять

Технические invariants — бинарные: полнота persistence, правильность refs, восстановление/изоляция, отсутствие ложного ready. Factual quality — полнота извлечения по независимо размеченному тексту, точность verdict, доля ошибочно пропущенных существенных assertions, количество critical/major factual defects на выпуск, citation applicability/independence и ошибки расчётов. Не делить число supported claims лишь на удобно извлечённое подмножество.

Narrative quality — воспроизведение главного ответа и причинной цепи слушателем, понимание ограничений, где он потерял нить, полезность примеров, повторы, темп, завершение и желание продолжить слушать. Audio quality — fidelity/произношение/естественность/стыки/утомляемость по05. Completion rate и доля ошибочного ready считаются по всем попыткам, а не только выпущенным: система, которая отклоняет всё, не получает статус качественной.

Фактчекеры оценивают источники, слушатели целевой аудитории получают только запись. Начальная политика предусматривает двух независимых фактчекеров/редакторов на case с разбором существенных расхождений и пять слушателей на полную версию. Размер выборки уточняется по разбросу результатов. В группе full_comprehension участник получает одну версию, слушает её целиком и затем объясняет услышанное. Парные предпочтения собираются отдельным заданием со сменой порядка вариантов.

Для **интереса** набирается другая группа. Участнику предлагают естественно продолжить или остановиться в любое время. Оплата и выполнение задания не зависят от полного прослушивания. Версии распределяются случайно при сопоставимом исходном интересе и знакомстве с темой. Голоса, громкость, формат и инструкция одинаковы. Сохраняются все назначения, включая не начавших. При остановке записываются момент, привязка к реплике при наличии и собственная причина участника.

В отчёте по каждой версии отдельно видны завершения, остановки из-за содержания, внешние прерывания, технические сбои, конец периода наблюдения и неизвестные причины. Для анализа времени до содержательной остановки внешнее прерывание, сбой или завершение наблюдения учитываются как цензурированные наблюдения на последнем известном frame, с раздельными численностями и фактической экспозицией. Неизвестную причину показывают отдельно и проверяют чувствительность вывода к её трактовке. Полное прослушивание по инструкции не засчитывается добровольным выбором. Эти измерения описывают будущий эксперимент.

Candidates anonymized, порядок рандомизирован, model/provider identities скрыты. Отдельно оцениваются scripted text и финальное аудио, чтобы приятный голос не скрывал слабое объяснение. Reviewer показывает evidence/точные места, а не только общий балл. Model judges калибруются против людей на calibration split, допускают abstain и не служат единственным ground truth. Position/verbosity/self-preference biases известны у LLM judges; их контролируют перестановкой вариантов, blind comparison и человеческой сверкой. [MT-Bench / LLM-as-a-Judge](https://arxiv.org/abs/2306.05685).

### Поведенческие anchors рубрики

Оценки 0–4 — порядковая шкала, не измерение вероятности качества. Anchors уточняются под аудиторию/язык на calibration cases; соседние категории сопровождаются примерами и правилами adjudication.

| Балл | Понимание слушателем | Связность и темп | Естественность исполнения |
|---|---|---|---|
| 0 | Восстановлен неверный центральный ответ либо ответ не понят | Невозможно восстановить путь объяснения; части противоречат друг другу | Исполнение мешает следить за речью на протяжении выпуска |
| 1 | Запомнены отдельные факты без ответа на вопрос | Частые необъяснённые скачки/повторы; слушатель постоянно теряет нить | Ритм/ударения/смены голоса регулярно отвлекают и разрушают фразы |
| 2 | Ответ назван, но существенный механизм или ограничение потеряны | Общая линия понятна, но несколько важных переходов приходится достраивать | Речь понятна, но повторяющаяся механическая подача мешает длительному слушанию |
| 3 | Верно объяснены главный ответ, существенные связи и ограничения | Переходы ясны, возвраты имеют функцию; остаются небольшие неровности | В основном естественная устойчивая подача; отдельные заметные артефакты |
| 4 | Дополнительно правильно разобран новый пример в границах объяснённого | Целый выпуск движется без лишнего повторного объяснения и завершает обещание | Устойчивая уместная выразительность; взаимодействие ведущих помогает смыслу |

Comprehension оценивается по свободному пересказу, вопросу о ключевом ограничении и одному заранее написанному transfer case; варианты ответов не подсказывают формулировку. Для темы без причинной модели rubric использует соответствующие связи/процедуру. Fidelity дефекты остаются отдельными blockers05: высокая естественность не компенсирует пропущенное отрицание.

Калибровка: подготовить экспертные reference episodes с проверенными источниками и разрешённым использованием, затем варианты с размеченными пропусками механизма/оговорки, лишними повторами и дефектами речи. Blind panel оценивает хорошие и испорченные версии по anchors; разногласия разбираются по фактическим местам. Уточнить rubric, повторить на новых calibration cases и зафиксировать минимальный приемлемый anchor, допустимую долю listener failures, non-inferiority margins и sample plan в versioned QualityPolicy. Начальный кандидат для понимания — anchor≥3, но его частота прохождения и пороги других метрик выбираются до holdout по этой процедуре. Пока policy не утверждена на calibration, profile имеет статус unqualified и не допускается к production. References не показываются generation model как ответы на holdout.

Три контрольные пары для калибровки текста:

1. Удалить из середины связку, объясняющую вывод финала. В план и ремарку оставить правильное объяснение. Первый читатель должен указать реальную реплику, где перестал понимать переход. В парном сильном тексте тот же вопрос намеренно отложен и вовремя закрыт. Проверить ложную тревогу и выбранный масштаб правки.
2. Взять сцену с работающим финальным возвратом и одной неверной датой. Сравнить точечное исправление даты с большой переписью, исправляющей ту же дату. Фактчек проводится по обеим версиям. Слепой редактор оценивает сохранность финала, голоса ведущих и полезных реплик. Прогнать также полностью корректный оригинал и проверить, сколько необязательных переписей ухудшили текст.
3. На одном подтверждённом материале подготовить информационный пересказ и версию с развивающимся вопросом. У каждой одинаковые факты, длительность и условия нейтральной читки. В диалоге сделать контрольную замену содержательного возражения дублирующей репликой. Отдельно измерить пересказ, первый потерянный переход, выбор продолжить и причины остановки. Направление различий определят результаты слушателей.

### Правило решения

До holdout фиксируются rubric, первичные метрики, practically meaningful improvement, допустимые границы regression и способ анализа. Эти численные границы выбирают на pilot/calibration, а не подгоняют после просмотра итогового кандидата. Episode/topic — единица сравнения; тысячи claims одного выпуска не изображаются тысячами независимых наблюдений. Отчёт даёт paired differences, разброс, интервалы неопределённости и случаи провала; при bootstrap повторно выбираются topic clusters, не отдельные фразы.

Кандидат квалифицируется только при выполнении технических invariants, отсутствии observed critical factual/fidelity false passes на qualification suite, приемлемых completion/understanding/audio results по закреплённой rubric и отсутствии значимого ухудшения обязательных групп. Ноль обнаруженных critical defects не доказывает нулевой риск; сохраняется оценка неопределённости. Inconclusive означает дополнительные случаи или отсутствие основания заменять baseline, а не победу более сложной схемы.

Если предложенный процесс не улучшает конечный результат, сравнить ablations и упростить модельную организацию; версионирование/evidence/release contracts сохраняются. Изменение prompt/tool/schema/model/extractor/TTS требует релевантного component regression и контрольных полных выпусков. Holdout не используется для пошаговой настройки: после раскрытия результатов нужен новый закрытый набор. Canary cases проверяют model alias drift; отрицательный сигнал отзывает qualification профиля до разбора, не переписывает историю прошлых результатов.

## 9. Реализация и приёмка

| Целевой пакет | Ответственность |
|---|---|
| `verification/domain` | ClaimSet, reports, findings, verdict/severity/policy, CalculationRecord, ShowNotesReview |
| `verification/extraction` | Claim/coverage tasks, qualifiers, omission audit, selectors |
| `verification/factcheck` | Source/context checks, counterevidence, reuse guards, report assembly |
| `verification/calculations` | RestrictedExpression interpreter, versioned modules/units, decimal execution |
| `verification/editorial` | Whole-script review, rubric, handoff и повторная оценка |
| `verification/release` | ShowNotes checks, acceptance/revalidation rules, eligibility request к06 |
| `verification/tools` | Удобные model-facing операции, IDs/refs через runtime |
| `evals` | Frozen cases, split ACL, repeated trials, human-review import, comparison reports |

Все пакеты находятся в `backend/src/podgen/`, кроме repo-level evals по06. Canonical artifacts и acceptance/revocation хранятся в общих repositories; searchable finding projections не заменяют immutable report. Trace обеспечивает воспроизведение реально показанного reviewer текста. Prompt/examples/model profiles versioned, не прошиты в workflow.

Обязательные приёмочные сценарии:

- Extractor пропускает отрицание, causal link и предпосылку вопроса: omission audit находит их; coverage не становится доказательством полноты по одному заполнению диапазонов.
- Один claim имеет верное число, неверный знаменатель и неправильный период: отдельные checks не позволяют supported по совпадению цифр.
- Два зеркала одного документа не считаются независимыми; неизвестная семья остаётся unknown.
- OCR меняет 12 на72: original inspection обнаруживает расхождение; новый span/representation, прежний evidence не patch-ится.
- Уточнение после critique проходит повторную global review; исправленный факт не скрывает новый непонятный переход.
- После удаления неверного факта новый script получает explicit dependency disposition и не наследует вечную invalidity от parent lineage.
- Новый report не принимает старый script по сходному названию акта; delayed results/cancel/race не создают ready.
- Supported hypothesis с слышимыми ограничениями проходит; то же ограничение только в show notes не оправдывает безусловную речь.
- Готовый script плюс аудио с пропавшим отрицанием блокируется05; transcript по исходному тексту не маскирует дефект.
- Заголовок добавил причинное обещание, которого нет в script: ShowNotesReview требует изменения; новый master пересчитывает главы/таймкоды.
- Fault injection в report save/acceptance/release commit повторяется без дублей, потери текста и смешивания revisions.
- Скрытые annotations не доступны generation worker; cached repeats и провалы не исключаются из benchmark молча.

Готовность подсистемы означает работающий цикл обнаружения → исправления → независимой повторной проверки, корректный выпуск точного комплекта и измеренную способность отличать хороший полный эпизод от убедительно оформленного плохого. Прошедшие schema tests или высокий средний model score отдельно этого не подтверждают.
