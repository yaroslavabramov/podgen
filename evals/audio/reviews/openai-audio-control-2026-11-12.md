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

Ближайшая доказательная проверка — **один audio-only запрос с PCM16 тишиной**, полностью тем же текстом, что у уже выполненного real PCM16. Не добавлять новых запретов/форматов/подсказок. Содержательный `NO_AUDIBLE_SPEECH` отделит отсутствие речи от реального содержательного аудио без опоры на reference. Пустой ответ или выдуманная речь — провал этой гипотезы.

Даже успех не квалифицирует full word-level QA: перевод вместо транскрипции остаётся несоблюдением задачи. Он только обосновывает дальнейшую проверку audio-first observations отдельно от сравнения. Пока не получен результат, production prompt и порядок работы не меняем, Snow не запускаем и никаких дополнительных semantic gates по status не вводим.

При провале этой последней узкой проверки разумен новый отдельный control для [gpt-realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1), для которого официально заявлены улучшения alphanumeric recognition и silence/noise handling. Это основание проверить кандидата, не обещание пригодности. Один успешный silence control ещё не доказывает чтение изменённого года в реальной записи.
