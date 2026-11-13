# Research tool contract: descriptions

13 ноября 2026. Прочитаны полный `writing/research_materials/tools.py` и базовые `source_tools/contracts.py`/`corpus.py`. Изменения tools принадлежат разработчику; этот разбор их не вносит. API и тесты не запускались.

**[P2] Сохранилось противоречие внутри `search_sources` schema.** Research-ветка заменяет верхний description, но наследует `parameters.properties.within.description`: там M называется карточкой, а null — поиском по всем primary sources. В новом входе M — раздел отчёта, а S может быть как первичным документом, так и вторичным объяснением. Следствие: модель получает разные значения одного аргумента в пределах одного объявления инструмента.

Предлагаемый точный текст **только для research-ветки**:

```text
Optional S identifier from source_catalog or search_sources, or M identifier from research_contents. S restricts the search to that retained document. M restricts it to readable documents cited by that report section; the citation is not verified support. null searches all readable retained source documents.
```

Параметры, типы и validation не меняются. Legacy definitions остаются прежними. Внедрять override нужно в deep-copied definition нужного инструмента, а не в общий класс Search.

Чтобы верхнее описание также не подразумевало историческую первичность или raw original bytes, предлагается:

```text
Search this episode's retained source documents, which may include contemporary records and later accounts. S identifiers match the report's final Sources numbers and may be sparse; source_catalog also lists unavailable sources. An M scope searches readable documents explicitly cited by that report section, not verified support. Search previews locate passages; read_source inspects their text and creates E receipts.
```

Два дополнительных уточнения описаний, без смены поведения:

1. `read_source.parameters.properties.source_id.description`:

```text
S identifier from source_catalog or search_sources. It identifies a retained source document, not a report section.
```

2. `read_material.parameters.properties.context.description`:

```text
Also read separately labelled report context around the returned page. Context has its own report ranges; it does not change the material_passage range or select adjacent materials for the writer.
```

Причина второго уточнения: текущий код берёт до 600 символов вокруг **страницы**, поэтому context может находиться внутри той же M-секции, а не только в соседней. Фраза «not part of this M selection» двусмысленна: полный M-раздел позднее передаётся автору целиком. Нужно различать диапазон текущей страницы и выбор разделов для автора.

Остальные прочитанные descriptions согласуются с текущей реализацией: M offsets локальны секции; `end=null` задаёт следующую страницу; R-reading отделён от E source evidence; `reported_sources` явно означает citations, не проверку поддержки. После prepare требуется проверить **полные фактические definitions в request**, включая descriptions вложенных свойств, и реальные continuation/context results. Одно исправление Python-объявления ещё не доказывает, что новое определение попало в frozen request.
