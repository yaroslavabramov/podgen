# Направленное исправление TTS: предложение

Прочитаны последние три хода research-чата `01a1b59f-b8ea-76b3-8473-fba1e06bc48b`, полный сохранённый Snow request и текущий repair `3710b6d`. Текущий repair повторяет frozen original request; описание дефекта остаётся в provenance. Это **verbatim retake**, а не направленная коррекция. Успешного live repair и акустического confirmation для Snow здесь не установлено. Код продукта, тесты и API генерации не запускались.

## Контракт

Нужна versioned надстройка существующего repair, без нового агента/режиссёра и изменений writing:

1. **Подтверждение:** существующий exact review result, `confirmed/audible_mismatch`, реально услышанные spans, parent audio hash и проверенная принадлежность целому synthesis fragment. Тип ASR-кандидата не заменяет акустическое решение.
2. **Target:** source utterance ID + Unicode code-point span `[start,end)`, точные `canonical_intended_words`, короткие before/after anchors. Для insertion — пустая ожидаемая вставка между двумя anchors; для omission — отсутствовавшие source words, а не слова, придуманные по ASR.
3. **Correction intent:** `restore_omitted_words`, `pronounce_canonical_words`, `remove_extra_or_repeated_words` либо `apply_verified_pronunciation`. Отдельная короткая English `performance_instruction` поясняет, что синтезатор должен исполнить иначе. Она не становится частью произносимого текста или сохранённого `base_style`.
4. **Pronunciation basis:** для специфического произношения имени/ударения нужны проверенная lexicon entry/audio reference и источник основания. При их отсутствии `needs_evidence/pronunciation_reference_required`. Нельзя выводить фонетику из ошибочного ASR, транслитерации по догадке или заменять написание имени в сценарии.
5. **Strategy:** ID, `supported|needs_evidence|unsupported`, exact qualification ref/fingerprint (model, voice, API surface/revision, adapter, instruction template, locale, defect kind). `needs_evidence` не разрешает полный repair-запрос. Смена модели/голоса и скрытый fallback не допускаются.
6. **Provenance:** original и corrected PreparedRequest/wire/hash сохраняются отдельно; source/script/hash, profile, base style и все текстовые поля неизменны. Разрешённое изменение instruction-поля перечислено явно. Submission отправляет frozen **corrected request**, иначе повторится нынешний ненаправленный retake. Новая версия контракта не должна называть изменённый wire «тем же запросом».

Разделение в коде: repair runtime связывает evidence/target/strategy; provider adapter компилирует отдельный `performance_corrections` в квалифицированный metadata-канал и возвращает новый PreparedRequest с mapping изменений. Текущий `SparseDirection.style` не менять: он задаёт базовую подачу всем репликам и не выражает локальный target. Submission использует обычный transport и новый frozen wire; validation не разрешает тихо проигнорировать instruction и повторить baseline.

`repair-directive.example.json` — конкретная, **неисполняемая** заготовка. В ней target «с семьями», utterance 0, span `[171,180)` и intent восстановить «с». Известен лишь Snow ASR candidate; `confirmed_evidence=null`, поэтому production repair по примеру запрещён. Реальный evidence сохраняет полный review result/clip, а компактная инструкция синтезатору передаёт действие и точную ожидаемую фразу; неправильное старое звучание не нужно повторять ему как образец.

## Что доступно, что требует проверки

Основание по Gemini — проверка root страницы [speech generation](https://aistudio.google.com/docs/speech-generation): text передаётся verbatim; документирован `speech_metadata.style` для prosody/emotion/pace, inline tags для pauses/non-speech. Страница не устанавливает поддержку фонем, pronunciation dictionary или lexical repair через style.

| Стратегия | Статус сейчас | Следствие |
|---|---|---|
| Тот же request, новый take | `supported` как retake | Поддержка отправки доказана текущим адаптером; эффективность адресного исправления не доказана |
| Literal correction intent в per-turn `style` | `needs_evidence` | Только короткий controlled capability probe; не полный шестиминутный вызов |
| Phoneme/dictionary/SSML pronunciation или `system_instruction` | `unsupported` в проверенном интерфейсе | Не добавлять выдуманные поля; не обещать такую возможность |
| Устойчивое неверное имя без произносительного основания | `needs_evidence` | Сначала надёжное pronunciation basis; фонетику не угадывать |
| Подмена source text «фонетическим написанием» | `unsupported` этим контрактом | Нарушает выбранный invariant дословного source; не маскировать под metadata |

`supported` для направленной стратегии появляется только после наблюдаемого результата probe, не из успешного HTTP или наличия `style`. Поддержка относится к проверенному способу и классу дефекта; не автоматически ко всем именам, числам и длинным фрагментам.

## Полное сравнение model-facing request

В `request-comparison.unqualified.json` сохранены **полные** baseline/proposed bodies. Baseline первой части: wire SHA-256 `d8569a6ceede93b10de258efa9496fb6249085899cef1a9f21f15069ea7feed1`, 18 реплик, Gemini `gemini-3.8-flash-tts`, `ru-ru-concierge-1`, `options={}`, исходный style пуст. Прочитан и второй полный Snow request: SHA-256 `b3baee6b02a36f67d25dc08c6513ed65e79af385c1c8e0a2e93bbb7fa4965562`; его 16 реплик в этом примере не меняются.

В proposed body меняется только `/input/0/content/0/annotations/0/style`. Все строки `text`, их порядок, model, speech_config/voice, generation_config и response_format остаются прежними. Базовый style в source/profile сохраняется; в wire появляется отдельная repair-инструкция на уровне metadata этой реплики. **Применимость этого канала к lexical correction пока не установлена.** Другие provider fields не добавлены. Это envelope для ревью, а не готовый API request: `full_fragment_submission_allowed=false`.

Полный новый instruction text:

> Correct the omitted word in “снимают с семьями на неделю”: say “с семьями” fully, including “с”. Speak only the supplied script, verbatim.

Ожидаемое изменение решения синтезатора — произнести конкретное source word в конкретном месте, которое было подтверждённо пропущено. Не «сделать красивее», не сменить эмоцию/темп и не пересказать фразу. Whole-fragment render scope пока остаётся целым исходным фрагментом; это нужно отдельно от направленности instruction и не обещает сокращения стоимости.

## Короткий controlled probe для root

- В отдельном evaluation источнике выбрать одну короткую исходную реплику с действительно подтверждённым дефектом. Если такого confirmation нет, проверка остаётся предварительной, не evidence устранения ошибки. Исходный сценарий не менять.
- Сравнить два коротких request: baseline без correction и candidate с одной metadata-инструкцией. Одинаковые text/model/voice/base style/options; если seed уже использовался — тот же, не добавлять seed или другие настройки ради сравнения. Полный шестиминутный request не отправлять.
- Независимо прослушать baseline и candidate. Проверить targeted words, сохранность остального текста, отсутствие произнесённой English instruction, повторов и навязанного нового темпа/стиля. Если baseline не воспроизвёл дефект, один правильный candidate не доказывает, что instruction его исправила.
- Для утверждения lexical capability нужны наблюдаемый baseline defect и исправление в candidate при достаточной повторной проверке. При неоднозначном результате оставить `needs_evidence`; при игнорировании/небезопасном для контракта поведении — `unsupported` в проверенной конфигурации. Не эскалировать автоматически к длинному render.

## Проверка repair, а не только нового ASR diff

Для каждого исходного подтверждённого finding сохранить обязательный **targeted recheck**: immutable source selector/canonical words + связь с новым render/assembly. Он выполняется даже когда новый ASR совпал со сценарием и candidate исчез. Время найти заново по новому аудио/ASR и anchors; старые frame offsets после перегенерации непригодны.

Reviewer получает новое реальное окно, exact reference и **новый** raw ASR/segments. Corrective TTS instruction, обещание «уже исправлено» и старое неправильное звучание ему не навязываются. Существующий нейтральный actual-audio prompt менять для этого не требуется; targeted recheck является обязательным case независимо от нового diff. Отсутствие надёжной локализации даёт более широкое окно или inconclusive.

Отдельно сохраняются:

- `original_defect_resolution=resolved|persists|inconclusive` с новой акустической evidence; правильный новый ASR сам по себе не даёт `resolved`;
- результаты full new-master independent ASR и review **новых** расхождений, включая другие места того же перегенерированного фрагмента;
- результаты проверки новых стыков, начала/конца и непроверенных участков. Исправление исходного слова не является принятием всего нового master.

Старая confirmation разрешает попытку исправления, но не подтверждает её успех. До recheck и проверки новой версии — `verification_required`, `production_ready=false`.
