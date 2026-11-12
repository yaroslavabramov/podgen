# OpenAI fallback: request и содержательная проверка контролей

Прочитаны полный нейтральный prompt, exact/altered payloads, полные отправленные bodies и raw responses, сохранённые root в `tmp/audio-openai-fallback-2026-11-12/`. Собственные API/тесты не запускались; production prompt не менялся.

## Request

`gpt-audio-1.5`, Chat Completions, native `input_audio` с base64 WAV, `modalities=[text]`, `store=false`, 8192 output tokens. Первый system message дословно совпал с принятым prompt. Второй задаёт только форму JSON через тот же WindowResolution schema; Structured Outputs параметр не используется. User payload совпал с fixture. Exact/altered получили одинаковый фактический WAV SHA-256 `366fe120cbde252d9ee8682e56be479578df28f3503cc98948ce61e5d36c8e3d`. Answer key, имена контрольных файлов и метка тишины в model-facing request отсутствуют.

Это соответствует документированным [audio inputs](https://developers.openai.com/api/docs/guides/audio-chat-completions) и [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create); у [gpt-audio-1.5 Structured Outputs не поддерживаются](https://developers.openai.com/api/docs/models/gpt-audio-1.5). Локальная валидация проверяет JSON, case set, статусы/reasons, обязательную heard evidence и границы окна; она не доказывает истинность услышанных слов.

## Реальные ответы — квалификация провалена

| Контроль | Ответ модели | Содержательный результат |
|---|---|---|
| Exact PCM32: reference 1970, ASR-приманка 1971 | dismissed/asr_error, «услышал 1970» | Ожидаемый ответ, но отдельно не доказывает использование аудио |
| Altered PCM32: reference 1972, та же приманка 1971 | confirmed/audible_mismatch, «услышал 1971»; также отрицает слышимое продолжение | **FAIL**: приманка перенесена в heard evidence. Правильный status не засчитывается |
| Цифровая тишина PCM32 с exact payload | dismissed/asr_error, «отчётливо слышно 1970» | **FAIL**: выдуманная речь |
| Altered PCM16, тот же prompt/payload | confirmed/audible_mismatch, «услышал 1971» | **FAIL** сохраняется после изменения encoding |
| Цифровая тишина PCM16 с exact payload | confirmed/audible_mismatch, «услышал 1971» | **FAIL**: выдумана ASR-приманка на нулевом PCM |

Исходная тишина проверена чтением frames: 288000 frames, 24 kHz, mono, весь PCM равен нулю, WAV SHA-256 `33593c4526ae5341d6d59d7aea3ee076cf2fb7578a7941388592e1d97835ad3f`. У реального клипа PCM ненулевой. В обоих PCM16 преобразованиях речь не синтезировалась заново; для реального контрольного клипа проверено exact equality нормализованных samples: каждый PCM32 sample равен PCM16 sample × 65536. Это свойство именно этого исходника, не гарантия lossless 24/32→16 для произвольного master.

## Узкая диагностика

Без reference/ASR/schema PCM32 real и silence дали пустой content и ноль output tokens. Тот же audio-only запрос с реальным PCM16 дал узнаваемое содержание (April 1970, Apollo 13, Lovell, Haise, Fra Mauro), однако перевёл его на английский вместо требуемой исходной транскрипции. Поэтому encoding влияет на наблюдаемое поведение, а одной контекстной предвзятостью весь сбой пока не объяснён. PCM16 тоже не исправил comparative reviewer.

Root выполнил оставшуюся audio-only PCM16 silence-проверку с тем же коротким запросом. В `hearing-only-silence-pcm16/raw-response.bin` модель выдумала «Bonjour, comment ça va?». Этот blind control также провален. Следовательно, проблема не сводится к видимой reference/ASR-подсказке; PCM16 и отсутствие подсказок сами по себе не квалифицируют `gpt-audio-1.5`.

Перевод вместо транскрипции у реального PCM16 остаётся отдельным несоблюдением задачи. Production prompt не менялся, дополнительных semantic gates по status не вводилось. Двухэтапный hearing не внедряем: его польза ещё не доказана, а самостоятельное blind observation тоже может быть ложным.

Root затем отдельно проверил [gpt-realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1), для которого официально заявлены улучшения alphanumeric recognition и silence/noise handling. Это основание проверить кандидата, не обещание пригодности.

## Дополнение: Realtime

Root сохранил `realtime-v2-altered/response.json`: модель вернула правильный год 1970, отвергла reference 1972 и ASR decoy 1971, добавила продолжение про Apollo 13. Однако завернула JSON в parentheses; strict parser отклонил ответ. Это содержательно согласованный ответ, а не product success. Год можно знать из истории, а продолжение присутствует в expected context, поэтому даже такая пара фраз отдельно не доказывает зависимость от аудио.

В `realtime-v3-silence` формат переведён на документированный [forced function calling](https://developers.openai.com/api/reference/resources/realtime/client-events): `submit_audio_review` с прежним WindowResolution schema, без `strict`/Structured Outputs. Полный neutral prompt сохранён, добавлена только инструкция вернуть результат вызовом функции. VAD выключен, output text, PCM16 mono24k; один response.create после audio commit. Tool schema не навязывает положительное решение и допускает inconclusive с пустыми heard spans.

**Содержательный контроль снова провален:** completed function call дал `confirmed/audible_mismatch`, heard «Апрель 1971 года.» (0–1.6 s) на цифровой тишине. В отправленном `input_audio_buffer.append` независимо проверены все 576000 байт: каждый равен нулю, PCM SHA-256 `30fafbdc0facf210a26498db982f7ff8ad1e0b9a805816731c72e356a768c162`. Reference/ASR payload соответствует exact control; метка silence и answer key модели не переданы.

Function calling устраняет проблему обёртки JSON, но сам по себе не исправляет выдуманное акустическое свидетельство. Причину не подменяем утверждением о якобы доказанном чистом prompt bias: audio transport и instruction following проверяются раздельно. Production prompt и смысл схемы этим чтением не менялись.

## Контроль на двух разных реальных записях и продуктовое решение

Root выполнил ещё два bounded Realtime v3 вызова с тем же function-calling режимом:

| Контроль | Содержательный ответ |
|---|---|
| `realtime-v3-altered`: реальный Apollo, reference 1972, ASR 1971 | Процитировал «Апрель 1970 года.», отверг оба предъявленных года; completed function arguments прошли обычный контракт |
| `realtime-v3-swapped`: реальное начало Snow, Apollo reference 1970 и ASR 1971 | Процитировал «Лондон, вечер третьего сентября 1854 года. Квартал вокруг Брод-стрит, недалеко от Голден-сквер.»; Apollo-годы отвергнуты |

У swapped **все non-audio wire events дословно совпали с v3-silence**. Это именно exact Apollo payload C1 (reference 1970), а не altered C2 (reference 1972); один отличающийся audio append содержит реальную запись Snow. Проверены raw PCM bytes против frozen PCM16 WAV, WAV/PCM hashes против manifest, 288000 frames mono24k, прежний neutral prompt и прежний WindowResolution schema. Подмена реального звука изменила услышанные слова вопреки обеим текстовым подсказкам — наблюдаемая зависимость от входного аудио есть.

В обоих реальных responses `output` содержит сначала commentary message, затем один completed function_call. Адаптер должен извлекать единственный `submit_audio_review` по типу/имени и валидировать его arguments; нельзя считать, что `output[0]` уже является function call. Сохранённые `result.output` совпали с raw arguments.

Принятое root продуктовое направление:

- `gpt-audio-1.5` не использовать как успешно квалифицированный reviewer этой задачи.
- Realtime v3 можно интегрировать **как диагностический reviewer реальных речевых окон**, затем отдельно содержательно прочитать его решения на Snow. Эти два контроля не являются полной квалификацией word-level QA, имён/отрицаний/стыков и не разрешают automatic repair или release acceptance.
- Exact all-zero preflight может вернуть `inconclusive/audio_unavailable` **без API**, с отдельным provenance `signal_preflight`, без поддельного model receipt. Проверять весь исходный decoded PCM окна **до** возможного квантования в PCM16: нули после lossy преобразования не доказывают точное отсутствие исходного сигнала. Не вводить threshold громкости, VAD или вырезание тихих слов.
- Все прежние silence failures остаются **FAIL**. Preflight не превращает их в пройденные controls и не доказывает поведение на ненулевом шуме, почти тихой записи или паузах внутри речи.
- Положительные словесные ответы сопоставлены с exact script и независимым ASR; вручную размеченного human gold здесь нет. Контраст реальных клипов доказывает использование различий звука, но не точность каждого слова/таймкода и не общую надёжность. Двухшаговый hearing/comparison пока не проектируется.
