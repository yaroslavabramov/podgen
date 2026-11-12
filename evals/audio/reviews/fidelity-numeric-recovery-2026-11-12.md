# Чтение numeric normalization и recovery

Прочитан рабочий diff integer alignment, token separators, ASR deliveries и current/result export. Сопоставлены сохранённые raw transcripts/candidates Snow и Apollo, затем новые `/4` артефакты после штатного reprocess, выполненного root. Самостоятельно backend-модули, тесты, reprocess и API не запускались.

## Закрытие находок

**P2 о невосстанавливаемых WAV после прерванного export закрыта чтением исправления.** `export_results.py` теперь независимо verify/create каждый JSON и WAV, без пропуска клипов при наличии JSON. `immutable_export.write_bytes` проверяет SHA-256 существующего файла, отсутствующий публикует через fsync temp + atomic rename под файловым lock. Несовпадение даёт conflict. Current pointer записывается после обхода файлов. Валидный JSON больше не считается признаком полного архива.

Изначально замеченное ложное integer equivalence «сто ноль» → `100` **исправлено во время чтения**: `integers.parse` теперь оставляет многословные формы с «ноль» literal. В реальных Snow/Apollo такой формы не было; это был статический counterexample к правилу, не установленная потеря слова в их отчётах.

## Реальные места и сохранность различий

| Материал | Чтение новой проекции |
|---|---|
| Snow: «пятьсот тридцать пять» / `535`, «двухсот тридцати» / `230`, «восьмисот девяноста шести» / `896` | Целые значения совпадают; явные case aliases и грамматика позволяют representation equivalence. Полные исходные tokens/ranges остаются в `normalization_equivalences` |
| Snow: даты «тридцать первого августа», «двадцать пятого ноября» | Day ordinal equivalence ограничена month context и диапазоном 1–31; она не превращает произвольное порядковое слово в cardinal |
| Apollo: «двадцать четыре» / `24`, «двадцать часов» / `20 часов`, «до пятнадцати» / `до 15` | Допустимы равные integer surface forms; слова единиц и surrounding scope остаются отдельными токенами |
| Apollo: «94 с половиной» / `94,5` | Не входит в integer rule; различие сохраняется для проверки |
| Apollo: `0,2`, `0,1`, `1,8` против раздельных segment forms | Decimal separators не стираются; transcript/segment conflicts сохраняются отдельно |
| Snow: «Сноу» / «снова он», «бутыль» / «бутыли», «жидкость из» / «жидкости» | Не numeric vocabulary; подавления нет |
| Snow: «не» / «ни», «и не» / «ни»; Apollo: «соединяют со» / «соединяется», «наденут» / «надет», «на вес» / «навес» | Смысловые/словесные расхождения остаются literal differences; это candidates, не подтверждённые дефекты речи |

Изменённые значения не имеют одинакового integer key. Знаки, десятичные/дробные/диапазонные формы и leading zeros не проходят simple unsigned canonical-digit ветку. Word grouping не пересекает исходную реплику, новую строку, тире или пунктуационный separator. `under_thousand` требует совместимого набора case aliases, а не стеммит незнакомые формы; неоднозначная одиночная форма без явного numeric context может остаться кандидатом. Это консервативный остаток, не ошибка TTS.

## Stale current и retry

Главный stale-report дефект закрыт в прочитанном алгоритме: `current.json` и текущий `candidates.json` зависят от последней processing, связанной с последним receipt. После failed reprocess текущий `report=null`; старый успешный report доступен только как явно названный `last_successful_result` и immutable result archive. Он не считается текущим. ASR retry имеет отдельные authorization/delivery records, сохраняет предыдущие receipts/reservations и разрешён только после полного provider rejection; unknown и успешный receipt не дают такого retry. Самостоятельных повторных сетевых вызовов этим ревью не выполнялось.

Фактические `/4` результаты после root reprocess: Snow **41 candidates + 27 явных integer equivalences**, Apollo **19 + 3**. Все сохранённые equivalence-пары прочитаны; ложного подавления на этих двух материалах не найдено. В Apollo остались имена, «94 с половиной»/`94,5`, словесные различия и шесть raw/segment conflicts. В Snow остались имена, окончания, отрицания и неоднозначные числовые формы. Счётчик ASR submissions по-прежнему **1** для каждого; сохранённый wire hash совпал с frozen manifest. Старые successful reports доступны в result archives отдельно от текущего результата.

Directed correction по-прежнему `needs_evidence`: сокращённый English instruction и единый action ID согласованы в proposal; live qualification нет. Integer equivalence и успешное восстановление processing/export не подтверждают произнесённые слова или успешность repair.

Открытых actionable находок в прочитанной версии не осталось.
