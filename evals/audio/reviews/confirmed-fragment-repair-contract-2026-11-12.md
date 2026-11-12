# Чтение рабочего контракта whole-fragment repair

Прочитаны `repair/contracts.py`, `parent.py`, `evidence.py`, `prepare.py`, `processing.py`, `subject.py`, `replacement.py` и связанная поддержка `AssemblyConfigV2`/`SynthesisPart`. Это рабочие, ещё не зафиксированные разработчиком файлы. Service/CLI/export repair на момент чтения не появились; отправка запросов и её лимиты этим чтением не квалифицированы. Код не менялся, API и тесты не запускались.

## Находка

**[P2] Привязка confirmed case к заменяемой части допускает неверную сторону стыка.** `backend/src/podgen/runtime/evaluation/audio/repair/evidence.py:33–38` требует только непустое пересечение source utterance IDs. Если один diff opcode содержит selectors двух соседних частей, обе проходят guard. При этом подтверждающие `heard_spans` могут полностью находиться во второй части, а конфигурация первой всё равно будет принята и разрешит её повторный синтез. Для insertion на стыке fallback anchors также принадлежат обеим частям. Текущий guard не сопоставляет акустическое свидетельство с frame map выбранной части.

Нужно явно разрешить принадлежность evidence целевому фрагменту, а неоднозначный случай через стык отклонить либо оформить как repair нескольких целых фрагментов. Одного совпавшего utterance ID недостаточно. `complete_case_resolution=pending_new_audio_qa` правильно запрещает объявить дефект исправленным, но не предотвращает генерацию непричастного фрагмента.

## Что в контракте связано корректно

- Parent subject и target part должны принадлежать одной сборке; review должен быть зарегистрирован, ссылаться на тот же subject и тот же audio hash.
- Требуется конкретный последний validated window result, candidate alias переводится обратно в исходный candidate ID. ASR candidates и отсутствующий/503 reviewer result не являются confirmation.
- `evidence.py:23` отдельно отклоняет `recognition_uncertainty`/representation conflict. Для script discrepancy требуется `confirmed`; `WindowResolution` допускает для него только `audible_mismatch` и непустые heard spans в пределах фактического WAV. Это проверка формы и происхождения акустического решения, а не самостоятельное прослушивание кодом.
- Repair использует полный original synthesis fragment. Frozen request заново кодируется и побайтно сравнивается с исходным wire; сохраняются source ref/hash, profile, origin part, parent subject/part и repair depth. Сценарий не переписывается и произвольные несколько слов из master не подменяются.
- Replacement сохраняет число и порядок частей. Новая repair part должна указывать на соответствующие parent subject и parent fragment; чужую успешную генерацию подставить нельзя. Затем применяется обычная проверка полного source coverage.
- Повторная сборка имеет новую identity. Требуются новые full-master ASR, actual-audio QA и join review; `production_ready=false` сохраняется. Старый reviewer result не объявляет новую запись проверенной.

Успешных acoustic reviewer results для Snow этим ревью не установлено. Поэтому ни один его ASR diff не получил разрешение на repair от этого чтения.
