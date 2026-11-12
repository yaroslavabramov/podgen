# TonePerfect: восемь сохранённых контролей

Текущий вывод по исходному набору исправлен в [оценке](../../reports/2026-11-12-toneperfect-controls/assessment-supersession.json): контроль безударных «-ая/-ое» не используется для браковки. Исходные запросы и критерии ниже сохранены как история эксперимента.

Последующие проверки могут содержать 1–8 случаев на trial при прежнем потолке 10 credits. В export отдельно показаны `review`, `uncertain`, `no_correction`, прочие и неоценённые слова; баллы не превращаются в допуск. Подтверждение человека хранится отдельно от решения сервиса.

Для просмотра сохранённых фрагментов без API:

```sh
python3 evals/operator/toneperfect-controls/listening.py \
  evals/reports/2026-11-12-toneperfect-snow-followup/listening.json \
  /absolute/path/to/local/listening-directory
```

Генератор проверяет хеши аудио, копирует исходные клипы и создаёт `index.html`. Сценарий и ответ сервиса раскрываются после прослушивания. Полные результаты 17 новых фраз: [results.json](../../reports/2026-11-12-toneperfect-snow-followup/results.json). 13 ноября пользователь подтвердил отсутствие ошибок во всех 17 новых фразах и наличие ошибок в двух прежних примерах. [Разметка](../../reports/2026-11-12-toneperfect-snow-followup/human-feedback.json) и [итог](../../reports/2026-11-12-toneperfect-snow-followup/human-evaluated-summary.json) отделены от неизменённых ответов сервиса: на чистом аудио получены два `review` и пять `uncertain`; шесть `partial`/неоценённых слов считаются отсутствием оценки, а не ошибками записи.

`trial.json` — вход developer operator CLI, contract `toneperfect-qualification/1`: фиксированный run_id, потолок 10 credits и ровно восемь `cases[]` с `case_id`, `audio_path`, `audio_sha256`, `reference_text`, отдельными `provenance`/`evaluation`. Соответствие прочитанному `runtime/evaluation/toneperfect/contracts.py` проверено по полям без импорта или запуска продукта. `manifest.json` — дополнительный индекс assets, contract `toneperfect-saved-controls/1`, с длительностями, хешами и плановым максимумом **8 credits**. Credentials и API здесь не используются; live выполняет root.

Provider request содержит файл `audio` с нейтральным именем `audio.wav`, `text`, `language=ru-RU` и явный документированный default `task=general`, который задаёт developer adapter. Control IDs, provenance, ожидаемые решения, human confirmation, Fish/OpenAI transcripts в request не включать. Для одной попытки сохранять один Idempotency-Key; повтор не должен превращаться в новый оплачиваемый контроль. Fallback выключен adapter header, чтобы не сравнивать разные калибровки незаметно.

Assets: `tmp/toneperfect-controls-2026-11-12/`. Каждый control directory содержит `audio.wav`, дословный `reference.txt`, минимальный `payload.json` и отдельный `provenance.json`. Общие provenance и полные source manifest snapshots сохранены рядом. `evaluation-only.json` — отдельная разметка, не provider input.

| Controls | Аудио | Длительность | Разница references |
|---|---|---:|---|
| feminine-exact / feminine-altered | Snow master 68.30–72.70 s | 4.40 s | Только «главная подозреваемая» → «главное подозреваемое» |
| date-exact / date-altered | Snow master 28.75–36.95 s | 8.20 s | Только «первое сентября» → «второе сентября» |
| word-boundary-split / word-boundary-joined | Snow master 103.55–109.50 s | 5.95 s | Только «по тому» → «потому» |
| silence-with-text | Созданный локально нулевой PCM | 4.40 s | Полное предложение Snow про подозреваемую |
| apollo-with-snow-reference | Первые два предложения Apollo, 0–5.70 s | 5.70 s | Аудио Apollo с предложением Snow про подозреваемую |

Snow извлечён как точный contiguous PCM24 mono48k из принятого master без gain, fades, resampling или склеек. Apollo — точный PCM32 mono24k диапазон прежнего 12-секундного frozen clip; upstream hash и source binding сохранены. Внутри каждой пары WAV побайтово одинаков. Reference соответствует выбранному целому предложению, а не многопредложной исходной реплике. Границы выбраны по сохранённым ASR timestamps с запасом в паузах; человек точность этих границ не подтверждал.

Human confirmation относится только к «главная подозреваемая» и «первое сентября» в том же исходном Snow PCM. Полные предложения, соседние слова, точные timestamps и W5 не являются human-verified gold. Тишина имеет отдельную детерминированную проверку all-zero PCM.

До чтения результатов зафиксированы критерии в `evaluation-only.json`: целевые word decisions вместе со scores, omissions, phones и `heard_by`; отсутствие ошибочной коррекции на двух human-confirmed targets; различение изменённых окончаний и дня. Одного overall либо небольшого изменения балла недостаточно. Тишина не должна приниматься за успешное чтение. Wrong-subject может выявляться содержательными warnings и word evidence, без обязательной HTTP-ошибки. Для пробела допустима согласованная оценка или unresolved; эта пара не сертифицирует произношение. Порог по результатам не подбирать.

Сохранять raw response/headers, word rows, warnings, model_version, engine_route, billing и исходный request. Разные route/version внутри пары — confound, без автоматического повторного запроса. Результат не разрешает repair/release и не оценивает интонацию или монотонность.

По [документации TonePerfect](https://api.toneperfect.app/docs) один credit покрывает до 15 s, поэтому каждый clip стоит максимум один credit; 8 контролей укладываются в 8. Все references короче 40 слов и clips короче 15 s. Для [русского](https://api.toneperfect.app/languages/russian/) phone-level diagnostics экспериментальны, отдельного prosody score нет. Документация проверена 2026-11-12; фактический charge/route читать из ответа.
