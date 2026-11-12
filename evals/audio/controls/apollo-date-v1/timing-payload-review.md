# Timing diagnostics и компактный reviewer payload

Чтение `cb520f6` и появившихся рабочих adjudication-модулей. Код не менялся, тесты и API не запускались. Хеши прочитанного кода — в `timing-payload-review.snapshot.json`.

## Разделение принято

В полной записи 27 zero-length segments после перевода в frames. Ранее упомянутые четыре — только те, которые попали в шесть числовых конфликтов transcript/segments; это не полный счётчик нулевых интервалов.

`timing.py:18–21, 35–39` исключает zero-length segments из anchors, сохраняя исходную диагностику. `timing.py:40–55` теперь привязывает segment и к неравному opcode: числовой конфликт получает локальный transcript range, а не весь сценарий. `candidates.py:33–45` создаёт review cases только из текстовых конфликтов; timing issues остаются в `recognition_uncertainties` и прикрепляются к затронутым cases как `timing_uncertainties`. Поэтому 27 совпавших слов не требуют отдельных word-review вызовов. `localization_status=uncertain` и `production_ready=false` не дают принять такую локализацию за доказанную.

Это разумное разделение диагностики локализации и проверки слов. Удалять raw segments, автоматически dismiss совпавшую речь или превращать uncertainty в семантический defect не требуется. Полное принятие выпуска по отсутствию word cases по-прежнему недопустимо.

Прочитанный сохранённый export пока `/2`: 49 cases = 16 script diff + 6 segment conflicts + 27 timing cases. Прогноз для `/3` — 22 word/segment cases и сохранённые 27 timing diagnostics. Это вывод чтения кода, не выполненный здесь reprocess.

## Что завершить в model-facing проекции

**[P2] Сейчас теряется явная область сравнения для insertion.** `adjudication/payload.py:25–31` не передаёт `operation`; для чистой вставки expected tokens и selectors пусты. Модель видит пустую reference и соседние абзацы без точного места допустимого соединения. Нужны `operation=insert` и два source anchors, между которыми нет ожидаемой вставки. Для deletion аналогично нужны услышанные соседние anchors; пустой ASR excerpt сам по себе не доказывает пропуск.

**[P2] Payload пока скрывает причину ненадёжной локализации.** `payload.py:18–31` не переносит `timing_uncertainties` и basis выбранного окна; `candidate_issue` содержит другой тип проблемы. Передавать компактный `localization` с basis и кодами relevant uncertainties. Полные диагностические записи остаются в manifest. Это данные для оценки достаточности окна, не новый semantic gate.

Остальная конкретная проекция для разработчика:

- Один приложенный WAV на window. В payload — `output_language`, измеренная `audio_window.duration_seconds`, cases. Hash, source refs, script hash, глобальные UUID и master offset сохраняются в immutable manifest.
- Model IDs — `C1`, `C2`, … с immutable alias map на persisted UUID. Модель отвечает теми же короткими IDs; проверить полный набор локальных IDs до перевода обратно в UUID. Текущий `CaseResolution.case_id: UUID` годится для persisted результата; ему нужен отдельный model-facing string ID/adapter mapping.
- `expected_words` — **точная подстрока source**, не массив tokens. `expected_context.before/after` — точные соседние source-подстроки с достаточным смыслом: единицы, управление, отрицание/scope и ближайшие anchors. Не три произвольных абзаца. Для insertion `expected_words=""` допустим только с двумя явными anchors. Положение gap и пустая reference различаются структурно.
- Определять локальный фрагмент по исходным char spans и соседним anchors: кандидат плюс ближайшая достаточная фраза/клауза. Если нужно больше контекста, расширить явно; не резать смысл по лимиту символов. Полный source/context сохраняется отдельно. Нельзя выдавать весь окружающий контекст за слова, которые обязаны поместиться в коротком WAV.
- `asr_transcript_excerpt` — точная raw подстрока; для insert/delete добавить компактные ASR before/after anchors. `asr_segment_excerpts` — слова исходных segments и **явно относительные к WAV** секунды. В примере `asr_segment_time_basis=seconds_relative_to_supplied_audio_window`. Исходные master timestamps остаются неизменными в receipt; локальные значения — отдельная производная проекция.
- `localization` — basis и названия relevant timing issues. Не передавать весь `candidate_issue` с повторными token dictionaries и техническими индексами, если те же смысловые данные уже представлены raw excerpt/segments. Для representation conflict достаточно явно сохранить обе формы.

[`compact-payload.example.json`](compact-payload.example.json) показывает конкретный пример окна 263.26–267.38 master: «Примерно через **94 с половиной** часа после старта». C1 проверяет source против raw `94,5`, C2 — тот же участок из-за segment forms `94`/`5`. Таймкоды в примере локальные; нулевая длина `5` сохранена. Пример не отправлялся reviewer и не является его ответом.

Согласованный English prompt менять для этой проекции не требуется: он уже требует actual audio, относительные heard spans, один ответ на case и inconclusive при недостаточных данных. Контроль даты приведён к коротким C1/C2, техническое соответствие вынесено в manifest. Акустическое ground truth не добавлялось.
