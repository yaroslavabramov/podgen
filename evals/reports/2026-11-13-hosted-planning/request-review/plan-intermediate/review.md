# Ревью промежуточного полного plan request

13 ноября 2026. Сохранён точный [preview](preview.json), SHA-256 `427c5a6ae265aa6c512a75672f92b3f01871bf58c6e012359f62682fea8cb37e`. `endpoint_called=false`. Прочитаны полные instructions, обе model-facing части input, все 62 записи source_catalog, tools с вложенными property descriptions и полная response schema. Это промежуточная версия до согласованных исправлений metadata/descriptions, не финальный допуск к запуску.

## Сравнение с baseline

- Instructions **точно совпадают** с согласованным `proposed-instructions/solo-plan.md`. Литературные абзацы сохранены; изменены только указания на dossier и добавлен блок доступа к M/R и S/E. Модель должна читать материал вместо выбора по карточкам, отделять исследовательский пересказ от источника и передавать автору точные M-ссылки. Новых требований к сценам, финалу или стилю нет.
- [Полный diff request](request.diff) сохранён против фактического rain-soil plan request. Это другая тема: замена assignment и самого материала не выдаётся за изменение поведения. Отдельно сохранены [части input](payload-parts.json), [собранный для чтения payload](payload.json), [tools](tools.json), [schema](response-schema.json) и [точный schema diff](response-schema.diff).
- Единственное различие response schema — enum `material_refs` в трёх местах: прежние M1–M7 заменены каталогом M1–M10. Все поля, descriptions, required, типы, решения и литературные требования схемы прежние. Набор разрешённых M соответствует десяти предложенным разделам.
- Модель `gpt-6.1-sol`, output limit 24576, reasoning medium, store/background false, truncation disabled и параметры cache прежние. Изменения `tools`, `tool_choice=auto` и `include=reasoning.encrypted_content` обеспечивают чтение планировщиком. Metadata адресует новый run, compiler/cache contract прежние. По текущему source_tool_contract лимиты остаются 12 model turns / 24 tool calls / 9600 read chars, финальный structured turn без tools.

## Материал во входе

Первая часть содержит только `output_language` и assignment: прямой пользовательский вопрос, solo, русский язык, 720–900 секунд, target 810, прежние audience/style/scope. `required_topics=[]`. Вторая часть — `research_contents` и `source_catalog`: нет полных карточек, отчёта или корпуса документов в initial payload. Нет `research_assignment`, подробного research_focus, человеческого `result-review` или задания исправить заранее названные ошибки. Исторические заголовки и источники пришли из самого отчёта.

Оглавление содержит полные доступные идентификаторы, длины, соседей и reported_source_ids. S-номера соответствуют Sources отчёта, включая недоступные и не полученные записи; техническое available не названо фактическим подтверждением. В этой версии каталога 15 available, 5 unavailable и 42 not_acquired. Возможность фактически прочитать материал и полнота передачи автору проверяются позднее по tools/results, а не по наличию заголовков.

## Известные замечания в этом snapshot

1. `search_sources.parameters.properties.within.description` ещё говорит о material card и all primary sources, вопреки M-разделам и неоднородным S-документам. Уточнения `read_source.source_id` и `read_material.context` также ещё не попали в фактические definitions. Точные формулировки — [tool-contract-review.md](../../tool-contract-review.md).
2. S39 пока `available`, coverage unknown, без предупреждения, что получены аннотация/metadata вместо полной главы. S49 пока назван служебной строкой о RMG Images, хотя сохранённый текст начинается с `# K1`. Оба исправления уже заявлены разработчиком; считать закрытыми их можно после нового prepare.

Новых изменений литературной части или schema вне согласованной области не найдено.

## Проверить в следующем prepare

В текущем `material-preparation.json` появились assessments S42/S49 со статусом partial по причине того, что это музейные карточки, не транскрипции архивных документов. Само ограничение полезно, но статус неполучения/неполноты нужно отличать от характера публикации. Полная полученная музейная карточка может быть available с limitation о пределах её свидетельства; S39 действительно является неполной заменой главы. Это замечание к следующей версии входа, а не утверждение, что эти assessments уже видны в сохранённом здесь request.

После правок перечитать итоговые definitions и metadata, затем подготовленные requests других ролей. Ни этот preview, ни отсутствие новых литературных изменений не заменяют чтение реального плана, отдельной рецензии, author handoff и итогового сценария. API и тесты рецензент не запускал.
