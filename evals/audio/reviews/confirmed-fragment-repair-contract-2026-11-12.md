# Чтение рабочего контракта whole-fragment repair

Прочитаны все появившиеся `repair/` модули, включая service/submission/attempts/verification/export/localization, CLI и связанная поддержка `AssemblyConfigV2`/`SynthesisPart`. Это рабочие, ещё не зафиксированные разработчиком файлы. Код не менялся, API и тесты не запускались. Выводы ниже получены чтением, не выполнением сценариев.

## Находка

**[P2] CLI теряет фактическую причину остановки до отправки.** `repair/service.py:34–41` возвращает `credential_missing`/`budget_exhausted`, но `audio_repair_cli.py:53–61` игнорирует возвращённое значение и печатает export. `repair/export.py:102–108` вычисляет статус только по уже существующей последней попытке. Поэтому при `max_requests=1`, одном сохранённом 503 и явном retry сервис остановится по лимиту без новой отправки, а CLI снова покажет `provider_rejected`. При отсутствии ключа первый run покажет `prepared`. Лимит расходов работает, но причина текущего отказа не попадает в пользовательский результат. Нужно сохранить/вывести outcome последнего действия отдельно от состояния прошлой попытки.

Предыдущая P2 о неверной стороне стыка **закрыта чтением исправления**: `localization.bind_fragment` переводит heard spans из окна в master frames с floor/ceil, проверяет sample rate и границы, требует одного владельца по точной frame map и совпадения с выбранным part index. Свидетельство в другой части и случай через несколько частей отклоняются. Время используется для выбора целого фрагмента, не для обрезки аудио.

## Что в контракте связано корректно

- Parent subject и target part должны принадлежать одной сборке; review должен быть зарегистрирован, ссылаться на тот же subject и тот же audio hash.
- Требуется конкретный последний validated window result, candidate alias переводится обратно в исходный candidate ID. ASR candidates и отсутствующий/503 reviewer result не являются confirmation.
- Запрет по `candidate.kind` удалён. Для любого case требуется `confirmed`; `WindowResolution` допускает для него только `audible_mismatch` и непустые heard spans в пределах фактического WAV. Representation case тоже может выявить реальное различие речи. Сам статус ASR-конфликта не является confirmation. Это проверка формы и происхождения акустического решения, а не самостоятельное прослушивание кодом.
- Repair использует полный original synthesis fragment. Frozen request заново кодируется и побайтно сравнивается с исходным wire; сохраняются source ref/hash, profile, origin part, parent subject/part и repair depth. Сценарий не переписывается и произвольные несколько слов из master не подменяются.
- Replacement сохраняет число и порядок частей. Новая repair part должна указывать на соответствующие parent subject и parent fragment; чужую успешную генерацию подставить нельзя. Затем применяется обычная проверка полного source coverage.
- Повторная сборка имеет новую identity. Требуются новые full-master ASR, actual-audio QA и join review; `production_ready=false` сохраняется. Старый reviewer result не объявляет новую запись проверенной.

## Отправка, resume и лимиты

- До HTTP сохраняются parent-fragment claim, prepared attempt и submit intent с reservation. Raw receipt сохраняется до parsing/decoding. На этом тракте transport имеет `retries=0`.
- Обычные run/resume при submit intent без receipt или при неполном receipt возвращают `outcome_unknown` и не отправляют запрос повторно. Полный сохранённый receipt обрабатывается локально; локальный reprocess тоже не вызывает провайдера.
- Явный `retry --reason` намеренно может создать новую paid attempt для failed/**unknown** состояния. Это не автоматический resend: нужен отдельный вызов, непустая причина, новая attempt identity и доступный лимит. В ledger остаётся reservation старой unknown attempt. Обычный resume и явный retry здесь различаются.
- Перед каждой отправкой проверяются `max_requests` (не более трёх) и `max_cost_usd` с reservation будущей отправки. Все submit intents считаются, включая отказы/unknown; known USD берётся как estimate, иначе остаётся reservation. Reservation repair не может быть ниже frozen profile envelope.
- Claim на parent subject + part не позволяет новым repair ID заново отправить тот же parent fragment. Configuration hash запрещает менять лимиты/исходные refs внутри того же repair ID. Depth наследуется от конкретной repair part, сохраняется origin part и ограничивается максимум тремя уровнями.

## Проверка новой версии

`prepare_verification` после локальной replacement assembly создаёт новый `FidelityConfigV2` на **весь новый final playback master**. ASR identity вычисляется из repair ID и нового assembly ref; прежний subject/audit ref удаляется из копируемой конфигурации. Сохраняются independent recognizer settings/price/лимиты, exact source binding и новый transport proof. Новый ASR только подготовлен, не выполнен автоматически.

Результат остаётся `verification_required`; next steps явно требуют new master ASR, actual-audio review и join review. Export использует отдельные каталоги для result versions и `current.json`, чтобы старая успешная версия не подменяла текущий результат после failed reprocess. Новую запись этот тракт не объявляет исправленной/принятой до её проверки.

Успешных acoustic reviewer results для Snow этим ревью не установлено. Поэтому ни один его ASR diff не получил разрешение на repair от этого чтения.
