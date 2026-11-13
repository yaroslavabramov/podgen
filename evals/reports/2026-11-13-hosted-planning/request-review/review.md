# Ревью полных requests ResearchMaterials

13 ноября 2026. Прочитаны восемь prepared previews из [runtime](../runtime/): plan, plan-review, plan-revise, plan-review-revised, draft, reader-review, fact-review, revise-1. У всех `endpoint_called=false`. Отчёт/документы реальные сохранённые; кандидат, approved plan, script и feedback — явно синтетические fixtures. Это проверка передачи и контрактов, **не** содержательная приёмка плана или сценария.

Полные промежуточные snapshots, их input parts, hashes и schema comparison сохранены в [all-roles-intermediate](all-roles-intermediate/comparison.json). Более ранний plan snapshot и полный diff — в [plan-intermediate](plan-intermediate/review.md). Текущие исправления ниже проверены повторным сравнением actual previews. Окончательные hashes восьми previews и полные request diffs зафиксированы в [preflight/manifest.json](preflight/manifest.json). **Подтверждённых незакрытых дефектов в этих подготовленных requests не осталось.**

## Что подтверждено

- Все восемь instructions в первом полном наборе совпали с согласованным адаптером и baseline. Для холодного читателя текст побайтно прежний. Литературные требования не менялись. Последующее уточнение revise описывает фактические `support_refs`/`source_read_refs` вместо отсутствующих карточечных excerpts; оно не меняет разрешённые операции редактур.
- В schema плана/рецензии изменились только разрешённые M-идентификаторы. Draft, reader и revise response schemas совпадают с соответствующим прежним production request. Fact schema первоначально отличалась только enum материала; затем в research-ветке исправлены два descriptions, указанные ниже. Поля, типы, required, decision enums и литературные descriptions прежние. Plan-revise сравнивался с baseline plan schema с учётом имени этапа.
- Модели и параметры прежние: plan/revise плана — gpt-6.1-sol, плановый рецензент и проверки — gpt-6.1-terra, автор/правщик — claude-opus-5-5; output 24576, reasoning medium. Порядок и лимиты правок не менялись. Новые tools доступны плановым ролям только через research input/version; legacy tools остаются прежними.
- Initial planning input содержит assignment, `research_contents` и `source_catalog`, без полного корпуса, KnowledgeCard, research_focus или human result-review. Задание не превращает исследовательский focus в required_topics. M-каталог и ссылки сохраняют происхождение от отчёта; S-номера соответствуют Sources, включая разреженные и недоступные позиции.
- Plan-review получает candidate и собственный доступ к материалам; plan-revise — candidate и существующее revision guidance. Ввод не содержит готовой человеческой рецензии на исследование. Из синтетического feedback нельзя делать вывод о качестве настоящего плана.
- В fixture выбран M3. В draft и revise переданы **все 2847 символов** его полного раздела, без пересказа или обрезки: SHA-256 `bd31e961f20ac8bb1d6c9e79ddd0f1e63de54f68317e203d91bd66f0e7d178db`. Текст один раз встречается дословно в frozen отчёте. Остальные M доступны через чтение, а исходные документы не загружены целиком в author payload. Общая передача нескольких выбранных разделов проверяется по реальному выбору будущего плана; одна fixture этого результата не доказывает.
- Fact-review получает `evidence=[]`, каталог 62 источников и оглавление, а не фиктивное evidence из M-текста. Access cautions описывают получение документов, не известные человеческие диагнозы ошибок отчёта. Проверенные источниковые passages должны возникнуть через S/E readings; R не выдаётся за evidence.
- Reader-review получает только язык, audience/genre/format, полный script, duration и собственный пустой initial audit context. Нет плана, report, source catalog, support, чужих reviews или tools. Script-only граница сохранена.

## Найденные несогласования и повторная проверка

| Находка | Исправление / статус |
|---|---|
| Вложенный Search.within говорил material card / primary sources; context смешивал страницу и выбранный M | Исправлено в полных definitions всех семи tool-enabled previews; теперь M означает раздел, S — retained document, context не меняет selection |
| S39 выглядел полной доступной главой, S49 получил служебный title; S42/S49 временно понижались до partial из-за жанра документа | В актуальном каталоге S39 partial с явным summary-only limitation; S42/S49 available; S49 title=K1 из retained heading. Первичность не подменяет completeness |
| Approved-plan fixture содержала legacy limitations/limitations_to_voice и протащила их в author story | Fixture исправлена. Повторный draft diff содержит только удаление этих двух полей; production schema/литературный текст не менялись |
| Fact schema продолжала требовать card IDs и разрешала [] «when original cards suffice», вопреки evidence=[] и новому prompt | Research-only schemas adapter добавлен с pin; в обновлённом actual fact preview изменились только два descriptions. Main/verification используют одну адаптацию семейства, legacy schema не меняется |
| Revise prompt ссылался на evidence_refs вместо фактически передаваемых support_refs/source_read_refs | Собственный adapter уточнён в `caaaddc`; пересобранный actual revise preview совпадает с ним полностью |

Точные два descriptions для fact schema:

```text
support_labels: Exact offered material IDs locating the report sections relevant to this finding; these references are not source evidence.
source_read_ids: Exact successful current E-read aliases or retained H-evidence aliases supporting this finding and its correction. Empty only when the necessary source evidence is already supplied in the current packet. R report-reading receipts are not source evidence.
```

## Nested references и границы проверки

В revise **response schema** нет вложенных карточечных evidence descriptions: структура операций прежняя. В фактическом handoff-коде M-ссылки без `source_excerpts` попадают в `support_refs` с wire-ключом `card_id`; это идентификатор раздела, не создание KnowledgeCard. Доставленные E-пассажи передаются отдельно в `review_source_passages`, а task ссылается на них через P aliases в `source_read_refs`. Такой P alias — ссылка на уже переданный source passage, не R-reading отчёта.

Опубликованная revise fixture содержит только reader task и не демонстрирует factual handoff. Прочитан runtime test, где проверены M support_refs, P source passage и отсутствие R в retained evidence, но рецензент его **не запускал**. Этот предел нельзя выдавать за просмотр настоящего factual review и последующей правки.

Audience `General listeners; no prior topic knowledge is assumed.` в reader/revise совпадает с действующим production/1 fallback и preview/live путями при отсутствии отдельного audience pin. Это не новая потеря в research adapter; смена общей audience policy не входит в задачу.

## Перед запуском

Пересобранный revise request прочитан через полную дельту к прежнему snapshot: изменения ограничены описанием handoff; его instructions точно совпадают с текущим адаптером. Подготовленные requests не заменяют будущие вызовы чтения: ещё предстоит прочитать реальные R/E tool results, полный план, отдельную рецензию, actual selected materials, script и проверки. Холодное чтение и лимиты редактур должны остаться прежними и в frozen manifest. API и тесты рецензент не запускал.
