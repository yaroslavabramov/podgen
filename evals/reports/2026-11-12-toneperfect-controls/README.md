# TonePerfect: независимое чтение восьми контролей

**Текущая версия не прошла квалификацию для проверки сохранности текста Podgen:** намеренно неверные окончания «главное подозреваемое» приняты так же, как подтверждённые человеком «главная подозреваемая». Дату различает, пробел не превращает в коррекцию, тишину и чужой текст выявляет. Эти локальные успехи не компенсируют провал одного из заранее заданных обязательных контролей. По данным этого запуска полный Snow, automatic repair и release acceptance не обоснованы.

## Что проверено

Восемь сохранённых clips, один HTTP attempt на control, восемь HTTP 200; повторы отсутствуют. Независимо перепроверены multipart: только `audio`, `text`, `language=ru-RU`, `task=general`; нейтральный filename `audio.wav`. WAV и references совпали с frozen inputs; внутри трёх пар аудио одинаково. Provenance, evaluation-only gold и Fish/OpenAI transcripts в multipart не попали. Raw response сохранён; parsed `provider_response` полностью совпал с raw JSON.

Все семь речевых ответов: `multilingual-native-v1-ru-dd47176068cc`, primary / eu, fallback=false. У тишины тот же route, но model_version=null: это отказ от оценки, не результат другой скоринговой модели. Route/version confound в речевых парах не обнаружен. Потрачено **7 free credits**, осталось **43**, paid=0, лимит 10 соблюдён. Полные идентификаторы, хеши, receipts и транспортные проверки — в root-owned [run-audit.json](run-audit.json).

Критерии зафиксированы до вызовов в [evaluation-only.json](../../operator/toneperfect-controls/evaluation-only.json), inputs — в [trial.json](../../operator/toneperfect-controls/trial.json). Пороги после результатов не подбирались. Автор отчёта не слушал аудио, API и тесты не запускал. Human confirmation относится только к двум target phrases; не ко всему предложению, соседним словам или timestamps.

## Результаты по целевым словам

| Контроль | Реальные word decisions / scores | Оценка по исходному критерию |
|---|---|---|
| Feminine exact | «главная» 99.5; «подозреваемая» 99.0; обе no_correction, omission_suspected=false | Целевые слова приняты, с human confirmation согласуется. |
| Feminine altered, тот же WAV | «главное» 99.0; «подозреваемое» 99.0; обе no_correction, omission_suspected=false; warnings=[] | **FAIL:** намеренная замена обеих форм не выявлена. Overall у altered даже выше: 97.2 против 95.4; это не основание считать его лучше. |
| Date exact | «первое» 79.0, no_correction, sounds_heard=0.83 | Нет ошибочной word correction на human-confirmed target. Сервис при этом считает один phone пропущенным; полностью безошибочное phoneme recognition этим не подтверждено. |
| Date altered, тот же WAV | «второе» 70.6, review; r2.definite=true, acoustic_decision=review; несколько phone substitutions | **PASS локального различения дня:** неверный target явно отделён от exact. Overall почти не меняется: 93.6 против 93.8. |
| Word-boundary split | «по» 97.3, «тому» 99.5, обе no_correction | Нет коррекции из-за раздельного написания. |
| Word-boundary joined, тот же WAV | «потому» 99.5, no_correction | **PASS проверки согласованности:** оба написания приняты. Human gold артикуляции/ударения W5 нет; качество произношения не сертифицировано. |
| Digital silence + reference | assessable=false, unassessable, reason=no_audio; объект scores присутствует, все его значения — null; words=[]; 0 credits | **PASS:** на проверенном all-zero PCM успешное чтение не выдумано. Это не квалификация почти тихой речи или шума. |
| Apollo + Snow reference | completed, assessable=true; warnings content/content_mismatch_suspected; пять из десяти words review; overall=75.2 | **PASS обнаружения чужого текста** по согласованным warnings и word evidence. Не проход точности каждого word score: остальные пять слов всё ещё no_correction, например «сотни» 91.3. HTTP error не требовался. |

## Phones и raw не превращаем в независимое hearing

В feminine altered публичные `word_evidence.r2.acoustic_decision` тоже no_correction, definite=false. Численные risks отличаются от exact, но сервис не выдал требуемого различения. Эти значения нельзя задним числом превратить в новый успешный порог.

В «подозреваемая» и «подозреваемое» одна phone row публично восстановлена с `heard_by=transcript`: raw для этого звука содержит op=D, heard=null, score=0, тогда как публичный row — operation=M, heard=expected и высокий display score. Другие публичные rows нормализуют `heard_raw`, например j → i. Это наблюдаемые преобразования представления, не доказательство того, какие слова произнесены. Глобальная phonetic_transcription у пары одинакова; её также не считаем вручную размеченной транскрипцией. Конкретная внутренняя причина пропуска окончания одним экспериментом не установлена.

В Date altered substitutions и review согласуются с требуемым различением. В Date exact один phone D и sounds_heard=0.83 остаются ограничением измерения, несмотря на правильный word decision. Raw detector scores, публичные display scores и acoustic_decision — разные поля; их несовпадение не следует скрывать или подменять одним overall.

## Замечания вне human gold

В обеих Date references соседнее «первого» получает review, omitted=true и 0.0; «появлялась» — partial, assessed=false, unsupported_phonetic_symbols, хотя display score=99.5. В обеих Word-boundary references «о» получает uncertain и 18.6. Эти строки сохранены целиком. Они требуют отдельной сверки, но **не объявлены доказанными ложными замечаниями или TTS-дефектами**: человек подтвердил другие targets. Равным образом принятый target не сертифицирует все соседние слова.

## Сохранённые свидетельства и предел вывода

[target-rows.json](target-rows.json) содержит все 19 целевых публичных word dictionaries, соответствующие полные raw word/feature rows, а также шесть нецелевых flagged/unassessed rows. Для тишины сохранён фактический пустой массив, для wrong-subject — все десять reference words. Поля не обрезаны: phones/phonemes, word_evidence, heard_raw/heard_by, timings, исходные численные значения и дополнительные поля сохранены. Повторное чтение файла подтвердило равенство каждого выбранного dictionary исходному raw JSON. Compact JSON — извлечение свидетельств, не новый provider response; полные response.bin остаются по указанным путям в assets.

Зафиксирован отрицательный результат на обязательном морфологическом контроле этой версии и этих записей. Это не оценка общей точности TonePerfect на русском и не доказательство единственного механизма сбоя. Документированно [phone diagnostics русского экспериментальны](https://api.toneperfect.app/languages/russian/); интонацию и монотонность текущий набор не проверяет.
