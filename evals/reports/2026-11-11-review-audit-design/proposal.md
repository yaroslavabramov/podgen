# Проект reading-audit/1

Прочитаны turns `01a1cb09…` и `01a1cb06…`, полные действующие fact, cold reader и reader-recheck инструкции, реальные схемы и payload сохранённых вызовов. [Базовые запросы и хеши](baseline-manifest.json). Прежний совет обязательно менять контекстное «кто-либо» отозван: приемлемый исходный финал сохраняется как положительный контроль. Нового режима авторской переписи нет.

## Действующие противоречия и точная замена

Fact сейчас возвращает findings и общий listener_answer. Его описание прямо говорит `not a required coverage checklist`, а important_limits — `not a preliminary audit`. Для нового контракта эти описания нужно заменить: положительные решения и непроверенное покрытие теперь имеют отдельную структуру, а не скрываются в общем пересказе. Источник истины — фактическая schema после всех sourced overrides: сейчас они отдельно заменяют observation, requested_change, priority. Обновление одной базовой модели не гарантирует новой инструкции в wire.

Cold reader сохраняет оценку полного рассказа, голоса, понимания по порядку, переходов, повторов и финала. Aspect checks фиксируют решения, которые получились из этого чтения. Они не требуют одинаковой композиции, находки на каждый аспект или отдельной интерпретации после каждой сцены.

Reader-recheck `/2` уже передаёт точные задачи R/F и lossless прошлый текст, но не сохраняет полный первый аудит. Fact-recheck пока вообще не получает собственные прежние решения. Новая память дополняет эту связку у обеих ролей. Предыдущие asset и schema остаются воспроизводимыми.

## Prose для новых заданий

Это конкретные смысловые блоки для последующей интеграции в полные задания, не дополнительный набор поверх конкурирующих старых указаний. Окончательные имена полей сверяются с контрактом разработчика.

**Fact, first audit:**

> Audit the complete factual account, including its contextual implications. Record decisions on supported claims as well as claims needing correction, and distinguish them from what remains unchecked. Establish the subject, time, scope, attribution and causal meaning conveyed by the connected speech before comparing it with evidence. Do not broaden a context-bound statement into an assertion about all places or times unless the speech actually makes that extension. Keep evidence inspection, its result and any unresolved coverage explicit; an unchecked claim is not a checked contradiction. Checking dates alone does not check the chronology or causal account built from them.

**Reader, first audit:**

> Read the whole episode before deciding on repairs. Give an overall judgment of how its voice, rhythm and development work for this audience, grounded in its actual speech. Record decisions across development, clarity, transitions, repetition, ending and spoken voice or idiom, including what is already acceptable. Related aspects may depend on the same passage. These decisions describe this telling; they do not prescribe a standard episode shape or require a defect under every aspect. Gather the demonstrated required work in this first audit, without turning an equally effective alternative into a recording blocker.

**Both, bounded recheck:**

> Use your prior audit, the actual changes and the open assignments to verify the current version. Retain a previous decision when its contextual meaning and evidential basis remain applicable; unchanged wording alone does not prove unchanged meaning. Examine changed material, its joins and dependent passages, and any effect on the whole episode. For each prior decision, state whether it is retained, reassessed or no longer applicable and why. A new required finding must identify the prior check or unchecked coverage, or the genuinely new claim, and demonstrate the substantive omission, introduced error, changed context or changed evidence that makes repair necessary now. An alternative interpretation imposed on the passage, a preferred arrangement or optional polish does not supply that basis. A real substantial error missed previously may still require correction; prior approval is not immunity.

Fact maintains its own source-grounded decisions; reader maintains its own listening judgments. Neither treats the other role's diagnosis as independent source evidence. Reader evaluates execution and listening effects of F tasks, leaving verification against sources to fact-review. An author's completion report does not replace the actual before/after comparison.

## Смысл описаний новых полей

Описания должны кратко определять содержимое; критерии принятия решения остаются один раз в системном задании.

| Поле/смысл контракта | Предлагаемое английское описание |
| --- | --- |
| fact `claim_checks` | Contextual claims actually assessed in this version, including supported claims and claims needing repair, with their anchors, evidence and individual verdicts. |
| contextual sense | What this passage conveys about its subject, time, scope, attribution and causal relations to a listener following the surrounding account. |
| kind | The factual relation assessed, such as identity, quantity, chronology, causation or generalization. |
| evidence | Exact supplied evidence and inspected source passages supporting this assessment, including relevant limits of the comparison. |
| verdict | Result of the actual check at the recorded contextual scope; distinguish support, contradiction, missing establishment and work not checked. |
| rationale | How the inspected evidence bears on this contextual claim and the resulting decision. |
| coverage | Passages and consequential factual relations checked, plus work left unchecked and the reason. |
| reader aspect checks | Anchored decisions on how the complete telling develops, remains intelligible, connects its passages, handles repetition, ends and speaks in its assigned voice. |
| prior dispositions | Applicability of each supplied prior decision to the current version, with its retained, reassessed or no-longer-applicable status and reason. |
| late required basis | The linked prior check, unchecked coverage or new claim, the concrete change or earlier omission, and why it now requires repair. |

У inherited решения сохраняются полное содержание, смысловой scope и настоящая опора, а не только verdict. Межпроходная ссылка на свидетельство должна сохранять идентичность исходного чтения/версии источника: голый `E1` другого invocation может означать другой passage. Связанные контекстом неизменённые фразы могут потребовать пересмотра после соседней правки; автоматическое наследование по совпадению строки неверно.

`unchecked` не выдаётся за supported или за false. Существенный незакрытый пробел должен оставаться видимым в состоянии проверки; он не порождает автоматически задание автору добавить оговорку. Полный первый аудит не обещает всеведения и не сводится к механическому числу утверждений или покрытых слов.

## Контрольные пары и критерии

[Четыре пары полных сценариев](controls/cases.json) сохранены отдельными копиями; исторические тексты не изменены. Метки приемлемости, причины мутации и ожидания — только у оценивающего. Модель получает полный сценарий, штатное задание, те же материалы и доступ к источникам. В каждой паре изменён ровно один фрагмент, остальные реплики совпадают. Длительность пересчитывается штатно при подготовке запросов.

1. **Сноу, контекст:** сохранить прежний финал с «кто-либо» в масштабе расследования. В паре явное утверждение о первом наблюдении в мировой истории должно получить адресное фактическое замечание. Справка вне выбранного контекста сама по себе не превращает приемлемую версию в ошибку.
2. **«Аполлон», механизм:** сохранить нужное объяснение, как дуга поддерживает пакет и оставляет проход воздуху. Отклонить версию, где картон поглощает CO₂, а гидроксид лития держит пакет.
3. **Библиотека, дата:** сохранить февральскую пробу; отклонить её замену на апрель при неизменном последующем марте и источниках. Независимое уточнение «следующий месяц» не проваливает положительный тест целевой даты.
4. **Найм, исход и причина:** сохранить договорённость о встрече и возможность краткого общего итога. Отклонить выдуманное трудоустройство из одного уточнения графика.

Оценка по целевому месту, реально услышанному смыслу, evidence, необходимости и исполнимости поручения. Другие корректные замечания не делают целевой положительный контроль неуспешным. Общие keep/revise, наличие нужного enum или повтор ожидаемых слов сами по себе ничего не доказывают. Отдельно оцениваются полнота и честность первого аудита, включая причинные и временные связи.

Повторные контроли нужны в четырёх режимах:

- Тот же принятый контекст и источники, исправлено постороннее поручение: сохранить прежнее решение; не воскресить глобальную трактовку финала Сноу и не удалить допустимый итог найма.
- Внесено одно из явных искажений пары: связать новый required finding с изменением, а не с «не выполнено старое поручение».
- Существенное искажение было пропущено первым аудитом: разрешить позднюю находку с конкретной опорой и объяснением упущения, без запрета замечать неизменённые ошибки. Например, при принятом ранее искажении механизма «Аполлона» должна остаться возможность исправить его.
- Изменена зависимая реплика или фактическая опора: переоценить затронутое прежнее решение с точной причиной. Остальные решения сохраняются; смена формулировки предпочтения не считается новой опорой.

До live фиксируются окончательные requests, хеши, source bindings и критерии. После изолированных чтений нужен штатный полный запуск до двух согласованных решений на одной последней версии в действующем лимите. Память не передаётся автору: его вход содержит только актуальные обязательные `revision_tasks` и нынешний editorial frame. Автор, планировщик, модели и лимит не меняются.

Сейчас это проект: production assets, prose/schema modules и pointers ещё не изменены; API и тяжёлые тесты не запускались. После появления точных структур разработчика описания полей и полный wire будут согласованы с ними, затем подготовлены новые неизменяемые assets.
