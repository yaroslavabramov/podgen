# Проверка правила audio → intended script

Правка существующего `local-fidelity-review.prompt.md` уточняет объект сравнения: `confirmed` означает отличие аудио от `expected_words`/`expected_context`; совпадение со сценарием означает `dismissed`, даже если ASR неверен. Формат ответа, поля, payload, транспорт и модели не меняются. Few-shot примеры в prompt не добавлены.

Последняя проверенная инструкция: SHA-256 `204057d959905c41cc88d035772ec930d82f2a836a5e89e85f8f9705453d831c`; новая: `7b47aea0256bb0792987e4549d90d1be8f8fbb93c2de99437bb760587ffc424c`. Полный diff приведён ниже. Для Realtime остаётся прежний суффикс `Return the results by calling submit_audio_review.`. Прочитаны правки developer в `contracts.py`: descriptions status/reason/explanation задают тот же audio-versus-script критерий, heard fields требуют реально услышанные слова. Новых полей, статусов и semantic gates нет; version identifiers адаптеров меняются отдельно для provenance.

## Минимальная live-матрица: четыре запроса, 14.72 s аудио суммарно

Root подготовил frozen fixtures в `tmp/audio-review-semantics-2026-11-12/{feminine-exact,feminine-altered,date-equivalent,word-boundary}/`, из прежних окон W4/W4/W3/W5 соответственно. В каждом запросе один прежний case. Проверено: у feminine-altered изменено **только** `expected_words`; остальной payload и WAV совпадают с feminine-exact. Названия контролей и `evaluation-only.json` не передавать модели. В `baseline-prompt.md` при чтении оказался уже новый hash `7b47…424c`, поэтому этот файл нельзя считать сохранённой старой инструкцией: old prompt доступен в git на `1a972ef`, полный diff ниже сохраняет сравнение.

| Контроль | Intended / ASR | Ожидаемое решение и обязательное содержание |
|---|---|---|
| feminine-exact: W4-A, C4, 3.96 s | «главная подозреваемая» / «главное подозреваемое» | `dismissed/asr_error`; heard «главная подозреваемая». Объяснение сравнивает её со сценарием, а не подтверждает дефект из-за ASR. |
| feminine-altered: W4-B, C4, то же аудио | expected «главное подозреваемое»; ASR тот же | `confirmed/audible_mismatch`, heard по-прежнему «главная подозреваемая», отличие от intended среднего рода названо явно. Статус меняется из-за изменения expected, не из-за изменения услышанного. |
| date-equivalent: W3, C3, 3.40 s | «первое» перед «сентября» / «1» | `dismissed/equivalent_spoken_form`; heard «первое сентября», объяснение сохраняет день месяца. Совпавшего статуса без обоснованной heard evidence недостаточно. |
| word-boundary: W5, C5, 3.40 s | «по тому» / «потому» | При одинаковом звучании: `dismissed/equivalent_spoken_form`. Нельзя подтвердить дефект аргументом «произносится слитно»; для `confirmed` требуется назвать реальное отличие аудио от intended, а не пробел в ASR. |

Transport PCM SHA-256: W4 `4242470df60b4929d3e342434987643798a95ffce1b467c77ecf7f8925ade662`; W3 `a82d3783a2509c1ef888b222622a947f3b52130e4e89c0846fe1fc29c8a955b8`; W5 `67730028b917fba37d079d398882667253f918d51b218171b1e5860a70271c42`.

## Основания и неопределённость

W4: два blind запроса одного `gpt-realtime-2.1`, без script/ASR, сохранили «главная подозреваемая»: `evals/reports/2026-11-12-audio-delivery-audit/{blind,blind-original-transport}/response.json`. Транспорт второго совпадал с production. Словосочетание согласуется также с heard evidence original-task ответа, который ошибочно поставил confirmed из-за ASR. Поэтому локальная женская форма хорошо обоснована независимыми от текстовой подсказки выходами. Это **не human-verified gold** и не независимость двух моделей. Те же blind ответы меняют соседние «Вокруг» → «Пока» и «невинно» → «невинной»; эти слова нельзя объявлять проверенными или расширять на них контроль.

W3: исходный script «первое сентября» согласуется по дню с независимым Fish ASR «1 сентября»; прежний contextual reviewer тоже дал dismissed. Отдельного blind word-perfect вывода этого reviewer для окна нет. W5: Fish даёт «потому», script — «по тому»; это не доказывает акустическую ошибку. Независимого blind вывода о точной артикуляции/ударении W5 нет. Неясный звук честно остаётся inconclusive, а не считается успешно проверенным.

Позднее прочитан прямой ответ пользователя в координирующем чате `01a07e3b-bf31-7f52-8e9e-0ba159507b5a` на показанные W3/W4 и вопрос о двух фразах: «Да, обе фразы совпадают». Это отдельное human confirmation только «первое сентября» и «главная подозреваемая»; оно не сертифицирует соседние слова, таймкоды или W5. Сам автор отчёта аудио не слушал. На момент чтения `evaluation-only.json` ещё содержал pending; root должен сохранить подтверждение отдельно от reviewer request.

Эта пара W4 заменяет повтор Apollo exact/altered: она проверяет именно обнаруженный сбой на обычной морфологии. Apollo можно оставить историческим контролем; дополнительных запросов ради повторения его статусов сейчас не требуется.

## Чтение результата и сохранение

Root запускает четыре изолированных запроса без изменения доставки. Сохранить полные исходные и изменённый payload, весь wire request, исходный prompt и schema, raw events, response.done, receipt, usage и результаты; исходные экспорты не перезаписывать. Проверить heard words, explanations и status вместе. Если W4-A и W4-B получают одинаковые слова, но неправильную логику статуса, новая инструкция задачу не исправила. Если появляется `audio_unavailable` при прежнем подтверждённом вводе, проблема остаётся открытой; её нельзя объявлять устранённой правкой критерия.

После этих четырёх ответов сначала содержательно прочитать каждый результат. Сам по себе ожидаемый status, valid JSON или успешная доставка не являются прохождением. Не запускать остальные 32 окна, пока эти данные не дадут основания продолжать. Матрица не квалифицирует omissions, отрицания, имена, таймкоды, стыки или prosody. API и тесты автором этой правки не запускались.

## Полный diff к последней проверенной инструкции

```diff
diff --git a/evals/audio/listening/local-fidelity-review.prompt.md b/evals/audio/listening/local-fidelity-review.prompt.md
index 2d43bca..1af02ee 100644
--- a/evals/audio/listening/local-fidelity-review.prompt.md
+++ b/evals/audio/listening/local-fidelity-review.prompt.md
@@ -1,13 +1,13 @@
-Check only the supplied candidate discrepancies against the actual audio window. Use `output_language` for explanations; preserve case IDs and source quotations exactly. Reference text and ASR output are comparison data, not proof of what was spoken. Raw ASR transcript and normalized segments may disagree.
+Review only the supplied cases for spoken-word fidelity. Compare the actual audio with the intended script in `expected_words` and `expected_context`. ASR is a fallible recognition of the recording, supplied only to help locate a possible discrepancy. An ASR error is not a recording defect. Use `output_language` for explanations; preserve case IDs and source quotations exactly.
 
-For each case, report the words you can actually hear in `heard_spans`, with start/end seconds relative to the supplied window. Mark unclear speech explicitly. Never fill a gap with reference words or complete an unheard sentence from context. Use phrase ranges supported by the audio; do not invent precise word timestamps.
+Report the words you actually hear in `heard_spans`, with start/end seconds relative to the supplied window. Use phrase ranges supported by the audio; ASR words and timestamps do not establish what was heard. Mark unclear speech explicitly rather than completing it from either the script or ASR.
 
 Return exactly one result per case: `case_id`, `status`, `reason_code`, `heard_spans`, `explanation`. Each heard span contains `start_seconds`, `end_seconds`, `words`.
 
-- `confirmed`, reason `audible_mismatch`: identify the heard words and ranges supporting the difference. Confirm an omission only with audible surrounding anchors and enough context to establish the missing span.
-- `dismissed`, reason `asr_error`, `asr_representation_conflict`, or `equivalent_spoken_form`: the audio matches the intended words. State what you heard.
+- `confirmed`, reason `audible_mismatch`: the audio differs from the intended script. Identify the heard words and the intended words they differ from. Confirm an omission only with audible surrounding anchors and enough context to establish the missing span. Disagreement between audio and ASR alone never warrants this status.
+- `dismissed`, reason `asr_error`, `asr_representation_conflict`, or `equivalent_spoken_form`: the audio matches the intended script, regardless of what ASR says. State the heard words and why they match.
 - `inconclusive`, reason `unclear_speech`, `insufficient_window`, `audio_unavailable`, or `pronunciation_reference_unresolved`: state the unresolved evidence.
 
-Accept punctuation, casing and digit/word differences that preserve spoken meaning. Check numerical value, units, names, negation, scope, order and repetitions carefully. A decimal separator lost in an ASR segment is not an audible error by itself.
+Spelling, word spacing, punctuation, casing, and digit/word notation alone do not establish a difference in sound. Dismiss equivalent spoken forms. Check numerical value, units, names, negation, scope, order and repetitions carefully. A decimal separator lost in an ASR segment is not an audible error by itself.
 
 Judge words only. Do not rate general prosody or voice quality, reopen factual research, or propose script edits. Return the agreed structured results only.
```
