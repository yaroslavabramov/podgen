# 05. Производство и проверка аудио

Спецификация целевого продукта, обновлена 13 ноября 2026. Вход — проверенная точная [ScriptRevision](03-story-and-writing.md); выход — целостное аудио, транскрипт и проверка реально получившегося звука. Общие версии — [00](00-system-contract.md), допуск к выпуску — [04](04-verification-and-evaluation.md), исполнение — [06](06-platform-and-runtime.md).

## 1. Ответственность и путь выпуска

Аудиоподсистема отвечает за исполнение: голоса, произношение, интонацию, ритм диалога, монтаж, измеренную длительность и соответствие слышимого проверенному сценарию. Результат оценивается прослушиванием; успешный HTTP response, корректный WAV и хороший текст по отдельности недостаточны.

```text
accepted ScriptRevision → PerformancePlan → render units → AudioSegment candidates
→ segment QA → монтаж → AudioMaster → фактический Transcript → полный AudioQAReport
→ AcceptanceRecord → ReleaseManifest (04/06)
```

В production-синтез поступает `mode=production`, `completion=complete`, без `research_placeholder`, с действующими текстовыми допусками 04 на эту же revision. Evaluation fixtures и candidate profiles исполняются только в отдельном evaluation purpose по правилам §2 и 00/06. Stage directions и citations не становятся произносимым текстом. Audio worker не исправляет факты и не дописывает связки; содержательная правка возвращается редактору и автору, затем проходит повторную проверку.

Перед рендером проверяются также закрытые хвосты актов, совпадение версий досье и EpisodeSpec у принятого плана и сценария и проекция действующих `passage` и `central_answer` из 02/06 для этой ветки. Новый блокирующий запрос после допуска текста останавливает принятие зависимого результата до решения редактора. Последняя проверка выполняется при сборке выпуска. Текст для PerformancePlan берётся из упорядоченной `spoken_view` раздела 03, а ремарки передаются отдельно как указания к исполнению.

Для испытаний текста оценщики могут использовать закрытые нейтральные читки из корпуса 04, в том числе специально подготовленных слабых вариантов. Такие записи доступны только оценщикам. Выпускаемое аудио проходит обычный путь от проверенного текста до AudioQAReport.

Рабочая единица драматургии — сцена/связный разговор, техническая единица запроса — render unit. Их границы могут различаться. Один ведущий получает контекст мысли; несколько ведущих — обмен репликами с намерением, а не набор независимо начитанных предложений. Качество всех стыков и целой сцены проверяется после сборки.

Текущий пользовательский ремонт: человек указывает версию и примерное время → система предлагает одно-два предложения, точный диапазон и контекстный preview → человек одним действием подтверждает **этот proposal** и лимиты TTS/ASR → озвучка с соседними предложениями тем же голосом/настройками → ASR локализует середину → exact extraction и новая версия master → один цельный after-preview на замену. ASR обязателен для определения границ сгенерированной середины; его текстовое совпадение не служит оценкой правильности звучания. Вердикты ASR, TonePerfect и LLM о качестве не разрешают и не блокируют человеческий ремонт. Полный контракт — §16; приёмка и два случая Snow — §18.

## 2. Профили исполнения

Все предметные объекты ниже — `ArtifactRevision`; показан `payload`. `ArtifactRef`, `BlobRef`, immutable revisions и policies общие с 00/01/06. Run IDs, provider request IDs и attempts принадлежат runtime, не подменяют ArtifactRef.

```text
VoiceProfile = {
  provider, provider_voice_id, provider_revision_or_alias,
  model_profile_ref, language_locales[], voice_kind: stock|authorized_custom,
  authorization_ref|null, usage_policy_ref, limitations[]
}
PronunciationLexicon = {
  language, entries: [{entry_id, written_form, context_rule,
    spoken_form|null, phoneme_form|null, phoneme_alphabet|null,
    basis_refs[], ambiguity_notes[]}], version_reason
}
```

`speaker_id` из сценария связан с одной VoiceProfile revision на выпуск. Авторский характер ведущего, тембр и способ произнесения — разные параметры. У выбранной пары голосов проверяются различимость, одинаковая громкость и естественные переходы; громкость или гендер не заменяют редакционную роль. Для custom voice требуется действующее разрешение использования. Stock profiles — обычный путь продукта; создание voice clone не входит в скрытые действия генерации.

VoiceProfile и ModelProfile06 — чистые immutable configurations. `VoiceProfile.language_locales` и `ModelProfile.configured_languages` задают запрошенные языки, не квалифицированную поддержку. Профиль не содержит обратных ссылок на будущие audition/capability/evaluation результаты. CapabilityReport06 ссылается на exact profile и tested_configuration_fingerprint; для audio проверяет `multi_speaker`, `context_text`, `context_audio_ids`, `pronunciation_mode`, `control_tags`, `word_or_character_alignment`, `lossless_output`, `seed_behavior`, `retrieve`, `cancel`. Наличие параметра в API не означает поддержку конкретной моделью; проигнорированный язык/словарь не считается применённым.

Assembly/delivery/recognition/alignment profiles используют общий ProcessingProfile00 с соответствующим profile_kind; recognition ссылается на ModelProfile recognizer и его decoding settings. Decoder profile имеет profile_kind=media_decode; resample profile относится к assembly. Audio policy — Policy(policy_kind=audio) из общего PolicyBundle. Ref проверяет kind и subtype.

Квалификация: сохранить candidate exact profile → выполнить CapabilityReport/EvaluationResult и получить audition assets → записать отдельный `AcceptanceRecord(check_kind=profile_qualification, subject_ref=exact profile, reviewed_input_refs=[reports/results], assessment_scope=QualificationScope)`. Profile после тестов не перепаковывается и не меняется. Действующие accepted records с учётом revocations определяют явно проверенные `supported_cells` языка, роли, формата, длительности и сочетания голосов; независимые списки параметров не дают разрешения на их произвольные комбинации.

`EpisodeRun.purpose=evaluation` и pinned EvaluationCase allowlist разрешают испытание candidate без production qualification, но не обходят source/privacy/tool policy. Evaluation может пройти полный путь и сохранить внутренний evaluation ReleaseManifest; пользовательские ready/export запрещены. Purpose задаёт scheduler, не модель. Private/custom VoiceProfile квалифицируется в том же tenant project с evaluator ACL.

Generation получает безопасную qualification projection и только явно опубликованные operator-ом nonsensitive audition assets через `CatalogPublication.published_assets`00/06. Raw EvaluationResult outputs, скрытые annotations/gold и приватные образцы другого проекта недоступны через обход refs. Read tools возвращают разрешённые exact config refs и опубликованные BlobRefs/hashes, не полный корпус квалификации. Project-local private voice samples остаются под ACL своего проекта.

## 3. PerformancePlan и преобразование текста

```text
PerformancePlan = {
  episode_spec_ref, script_ref, verification_report_ref, editorial_review_ref,
  speaker_bindings: [{speaker_id, voice_profile_ref}], lexicon_ref,
  model_profile_ref, audio_policy_ref, delivery_profile_ref,
  scenes: [{scene_id, script_selectors[], dramatic_intent, listening_context,
    utterances: [{utterance_id, script_selector, spoken_text,
      mappings: SpokenMapping[], direction: {pace_intent, emphasis[], emotion, pause_after_ms|null}}],
    render_units: [{unit_id, utterance_ranges[], prior_context_selectors[], next_context_selectors[],
      context_mode, boundary_reason, payload_template_hash}]}],
  normalization_decisions[], unresolved_issues[]
}
SpokenMapping = {
  spoken_char_start, spoken_char_end, script_selector,
  transformation: identity|number_expansion|abbreviation|pronunciation,
  rule_id, lexicon_entry_id|null, equivalence_basis
}
```

`script_selector` использует 03: `{script_ref, selector:{act_id, block_id, item_id, char_start|null, char_end|null}}`; item_id разрешается в utterance_id. Внутренние текстовые offsets — Unicode code points, полуинтервал `[start,end)`. Provider-specific indexes переводит adapter с проверяемой mapping; собственные единицы провайдера не считаются автоматически code points.

Полный исходный текст остаётся в ScriptRevision. `spoken_text` — его явная исполняемая форма: «12 недель» → «двенадцать недель», общепринятая расшифровка сокращения, проверенное ударение. Mapping покрывает каждое произносимое слово. Она не разрешает незаметно менять число, оговорку, причинность, цитату или имя. Неоднозначное `1.234`, неизвестное сокращение и вариант имени с иным смыслом возвращают `normalization_requires_editorial_change`; выбранная новая формулировка сохраняется в 03 и проверяется 04. Свободное model paraphrasing под видом нормализации запрещено.

Пунктуация/ударения и фонемы могут отличаться технически, но решение о допустимой эквивалентности хранится с rule/lexicon revision. Финальный transcript показывает фактически произнесённые слова; привычное написание чисел допускается отдельным display mapping, не выдаётся за дословную ASR-гипотезу. Ударение в русском имени и произношение смешанных языков проверяются аудио, а не совпадением письменного имени.

Performance direction выбирает эмоциональную задачу и акценты, согласованные с текстом. Она не превращает оговорку в сарказм, вопрос в утверждение или предположение в уверенную декларацию. Существенное изменение воспринимаемого смысла возвращается в редакционную проверку. Режиссёрская модель получает полный акт, цель сцены и соседние реплики; источник остаётся данными, а управляющие tags компилируются только из allowlist.

Литералы, похожие на provider markup, экранируются адаптером либо приводят к явной несовместимости. Нельзя удалить кусок текста «для безопасности» и считать сценарий исполненным. Словари/phonemes/tags входят в exact request и render key. Автоматическая нормализация провайдера отключается, если capability это надёжно поддерживает; иначе её результаты сравниваются с ожидаемой spoken form, включая числа и отрицания.

`payload_template_hash` фиксирует план запроса до синтеза: содержание, настройки и способ получения соседнего контекста. Exact `request_hash` и `render_key` возникают при submit после разрешения уже существующих prior audio refs/provider IDs и сохраняются в runtime/AudioSegment. PerformancePlan не ссылается на ещё не созданный сегмент и не дописывается после рендера.

## 4. Выбор синтеза и границы запросов

Выбор начинается с пятисемейного сравнения по [исследованию озвучки от 12 ноября 2026](research/2026-11-12-speech-synthesis.md): ElevenLabs `eleven_v4`, Gemini `gemini-3.8-flash-tts`, Inworld `inworld-tts-2`, Fish `drama-3` и MiniMax `speech-2.8-hd`. Сравниваются одинаковые отрывки в исполнении двух русских голосов каждого семейства; основной профиль выбирается по реальному звучанию и сохранности текста. Первым доступным адаптером остаётся ElevenLabs Text-to-Dialogue с timestamp response на явно заданной `eleven_v4`; отдельно — solo TTS path того же семейства. Его API принимает список text/voice inputs и возвращает audio, voice segments и original/normalized character alignment; рабочий ориентир dialogue request — суммарно до 2 000 characters. Доступность точных моделей и бесплатных квот проверяется перед запросами; недоступные пробы фиксируются без покупок и подмены модели, частичный набор не объявляется завершённым сравнением пяти семейств. [ElevenLabs: dialogue with timestamps](https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert-with-timestamps).

Сцена делится после spoken normalization и добавления допустимых control tags. Счётчик ограничения относится к фактически отправляемому payload. Его единицу adapter берёт из документированного контракта и qualification probes; неоднозначное provider «character» не отождествляется с Python len/UTF-16 без проверки. Начальная внутренняя цель — до 1 600 provider-counted characters с запасом, только по границам смысловых обменов. Это калибруемая настройка. Ошибка вместимости меняет сегментацию; смысловой хвост не обрезается.

У этого API previous/future text ограничены и continuity options зависят от модели; request stitching также зависит от retention режима. Поэтому перенос полного контекста сцены через эти параметры не обещается. [ElevenLabs: request parameters](https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert-with-timestamps). Полный контекст доступен режиссёру; TTS получает только поддержанный typed context. Не поддерживаемый unspoken context нельзя добавлять в обычный text и надеяться, что голос его не прочитает.

Для последовательных units сначала синтезируется предыдущий контекст, если профиль использует его audio request IDs. Независимые сцены можно выполнять параллельно. При локальной перезаписи adapter использует доступный preceding/following context либо расширяет замену до целого обмена. Создание взаимозависимых requests, каждый из которых ждёт ещё не созданный следующий, запрещено.

OpenAI Realtime — отдельный кандидат адаптера для сравнительных испытаний fixed-text rendering; он не считается взаимозаменяемым с Speech API. Текущий официальный guide сообщает закрытие legacy `tts-1`/`tts-1-hd`/`gpt-4o-mini-tts` 6 января 2027 года и другой Realtime workflow для замены. Новый основной путь на закрывающемся API не строится. [OpenAI: text to speech](https://developers.openai.com/api/docs/guides/text-to-speech).

Квалификация сравнивает isolated utterances, contextual multi-turn units и целые сцены в пределах возможностей каждого adapter. Выбор делается по естественности разговора, сохранности текста, произношению и стыкам на тех же сценариях. Потоковый conversational speech, способный перефразировать текст, допускается только после проверки fidelity; красивый голос не компенсирует пропуски. Модель/голос выбираются по language/format/duration support matrix из 04, не по названию provider.

## 5. Рендер и AudioSegment

```text
AudioSegment = {
  script_ref, performance_plan_ref, scene_id, unit_id, take_id,
  voice_profile_refs[], model_profile_ref, lexicon_ref,
  expected_spoken_text, spoken_mappings[], context_refs[], render_key,
  provider_audio_blob, pcm_blob,
  sample_rate_hz, channels, sample_frames,
  provider_alignment_blob|null, decoded_alignment_blob|null,
  decode_report, source_invocation_ids[], reuse_basis_ref|null
}
```

`take_id` различает сознательные варианты исполнения одной unit; transport retry не создаёт новый художественный дубль. AudioSegment публикуется только после полного получения и декодирования audio; incomplete response остаётся diagnostic attempt. Наличие сегмента означает целостный медиаобъект, не допуск по звучанию. Ни QA, ни mutable status не встраиваются в immutable payload.

1. Проверить exact text approvals, episode epoch и отсутствие unresolved PerformancePlan issues; для production — действующую voice/profile qualification, для evaluation — pinned candidate allowlist и отдельные ограничения purpose из 00/06.
2. Сохранить render intent и полный provider request. Submit через runtime 06; provider ID/stream/ошибка сохраняются отдельно.
3. Получить весь audio в staging object, проверить checksum, container, decode termination и диапазоны длительности. Не доверять HTTP 200 с оборванным stream.
4. Сохранить исходные bytes; декодировать PCM без удаления пауз и изменения темпа. Native sample rate известен; дальнейший resampling не добавляет исходной детализации.
5. Сохранить AudioSegment, diagnostic alignment и выполненные технические measurements. Recognition для индексации остаётся отдельной операцией; локальный repair не ждёт полного ASR или listening-judge.
6. Selection record привязан к exact take/ref. Неполный или недекодированный результат не попадает в монтаж; сборка complete PCM сама по себе не означает принятие качества. Для локальной замены оно проверяется по §16.

Неуспешная unit не заменяется тишиной, повтором предыдущего фрагмента или другим голосом. Пауза — явное режиссёрское решение в плане, а не fallback. Продолжение соседних независимых сцен допустимо, но master остаётся незавершённым.

## 6. Монтаж и AudioMaster

```text
AudioMaster = {
  script_ref, performance_plan_ref, episode_spec_ref,
  segment_refs[], assembly_profile_ref, delivery_profile_ref,
  edit_decisions: [{segment_ref, source_start_frame, source_end_frame,
    timeline_start_frame, timeline_end_frame, resample_profile_ref,
    gain_db, transition, transition_frames}],
  pcm_master_blob, playback_blob, additional_deliveries[],
  sample_rate_hz, channels, sample_frames, measured_duration_seconds,
  playback_decode_map, processing_manifest_blob, measurements_blob
}
```

Canonical audio coordinates — целые sample frames от начала декодированного PCM master, origin=0, полуинтервал `[start,end)`. Frame содержит один sample каждого канала; это не сумма samples всех каналов. `measured_duration_seconds = sample_frames / sample_rate_hz`; time values UI производны. Source evidence timebase из 01 сохраняется отдельно при обращении к исходной записи.

`AudioSelector = {audio_ref:ArtifactRef, start_frame:integer, end_frame:integer}`. audio_ref имеет kind AudioSegment либо AudioMaster; диапазон относится к canonical decoded PCM именно этого artifact, с его sample_rate_hz и `0 ≤ start_frame < end_frame ≤ sample_frames`. Локальные sample_ranges в report/repair adapter дополняет точным audio_ref из subject; несовпадение с проверяемым subject отклоняется. Так DependencyDisposition00/06 адресует изменённое аудио без угадывания версии или timebase.

В edit decision source_* адресуют PCM указанного AudioSegment в его sample rate; timeline_* — PCM master в sample rate master. Resample/trim/filter delay учитываются mapping и проверкой границ, а не смешиванием этих индексов. ShowNotes.chapter.start_frame и Transcript master ranges используют только canonical master frames. Export/player переводит их через playback_decode_map.

Монтажный план содержит точный порядок segment refs, trim/gain/паузы и transitions. Запрещено разрезать слово по символам или вычисленному среднему времени. Trim допустим по проверенной границе речи; переход/crossfade не съедает согласную и не создаёт незапланированное наложение реплик. Если точная граница не установлена, пересинтезируется больший контекст. Речь по умолчанию расположена в центре; различимость ведущих достигается голосом/ролью, а не жёстким pan в разные уши.

Начальный delivery profile: processing float PCM, master WAV 48 kHz/24-bit, stereo; playback MP3 48 kHz/256 kbit/s. Эти параметры — проектные defaults. Native provider audio и промежуточные lossless buffers сохраняются; многократная lossy-перекодировка не используется. Другой формат становится отдельным delivery profile с собственной проверкой.

Начальная loudness policy: integrated target −16 LUFS, допуск ±1 LU, true peak не выше −1.5 dBTP на декодированном playback. Это политика продукта для калибровки, не универсальная норма подкаста. Сначала выравниваются явно мешающие различия ведущих, затем whole-program normalization; содержательные изменения динамики не исправляются произвольным сжатием. LRA/short-term loudness сохраняются как диагностика, не принудительная одинаковость всех сцен.

FFmpeg `loudnorm` поддерживает single/double pass и целевые integrated loudness/true peak; pipeline использует два прохода с измеренными параметрами, затем независимое повторное измерение файла. Применённый mode и actual measurements сохраняются; implicit fallback фильтра нельзя принять за выполнение выбранного режима. [FFmpeg: loudnorm](https://ffmpeg.org/ffmpeg-filters.html#loudnorm).

После encoding файл полностью декодируется. Проверяются duration, clipping/true peak, начало/конец, gaps и соответствие PCM timeline. Encoder delay/padding не угадываются: `playback_decode_map` хранит измеренное соответствие timeline выбранного playback decoder; seek проверяется в поддержанных browser players. Произвольная загрузка экспортированного MP3 в другой проигрыватель не обещает sample-accurate seek без его qualification.

`playback_decode_map` содержит decoder_profile_ref, encoded_blob_sha256, decoded_pcm_sha256, decoded_sample_rate/sample_frames и интервалы соответствия `{playback_start_frame,playback_end_frame,master_start_frame,master_end_frame,kind:content|padding}`. Padding явно отмечен; неизвестный сдвиг или drift — ошибка проверки, а не округление. QA слушает decoded playback; его timestamps возвращаются в canonical master frames через эту карту. Для дополнительного delivery сохраняется собственная карта и проверка.

Изменение montage/пауз/темпа меняет AudioMaster revision, его transcript и audio QA. Старые абсолютные таймкоды не переносятся. Если длительность вышла из EpisodeSpec tolerance, редактор выбирает содержательную правку/новую подачу либо предлагает изменение target; strict не обходится наполнением и механическим ускорением.

## 7. Фактический Transcript и выравнивание

```text
Transcript = {
  scope: segment|master, script_ref, performance_plan_ref,
  audio_segment_ref|null, audio_master_ref|null,
  sample_rate_hz, sample_frames,
  recognition_profile_refs[], raw_recognition_blobs[], alignment_profile_ref,
  recognized_audio_sha256, decoder_profile_ref|null,
  words: [{word_id, spoken_text, start_frame, end_frame, speaker_id|null,
    script_selectors[], recognition_status, alignment_status}],
  non_speech_intervals: [{start_frame,end_frame,kind}],
  unresolved_intervals[], display_mappings[], coverage_report
}
```

Для scope=segment заполнен только audio_segment_ref; для master — только audio_master_ref. Transcript не содержит AudioQAReport ref. QA позже ссылается на transcript, поэтому зависимость не циклическая. Sequence words монотонен внутри speaker track; допустимое перекрытие speakers явно размечается. Все интервалы находятся в пределах реального количества frames.

Если recognition нужна для локализации, recognizer получает **звук без ожидаемого сценария в prompt/hints**. Language, decoding parameters и model/profile hash фиксируются; raw text/segments сохраняются. Word timestamps и VAD — гипотезы локализации, не доказательство правильного текста. Для выбора исходного диапазона human repair используется сохранённая recognition либо timeline конкретного local_master; новый ASR всего master перед каждой заменой не нужен. В contextual flow отдельный короткий ASR локализует середину нового TTS-контекста после общего approval, не оценивая качество речи.

При построении полной recognition-карты охватывается вся итоговая запись; разбиение длинного master использует перекрывающиеся окна и точную mapping к исходному timeline. Слияние результатов не теряет слова на границах. VAD не выбрасывает интервалы из аудита: первоначально full-waveform pass без фильтра, затем speech/silence анализ; каждый исключённый interval имеет причину. Услышанная лишняя фраза не удаляется из transcript ради сходства с текстом.

ASR и ограниченные equivalent normalization rules помогают найти место сценария и предложить временные границы; WER, confidence и текстовые расхождения остаются диагностикой. Совпадение не доказывает правильность произношения, несовпадение — дефект записи. Числа, отрицания, имена и формы слов не переписываются ради совпадения. Человеческого указания места и подтверждения точного proposal достаточно для локального ремонта: повторное ASR, TonePerfect или LLM adjudication перед ним не требуются. Неуверенная локализация показывается явно; границу можно сдвинуть/расширить до подтверждения, а не вычислить по среднему времени слова.

После установления фактических слов aligner уточняет word/utterance boundaries. Forced alignment получает audio+текст и ищет соответствие; он не проверяет, что отсутствующее слово было сказано, и не является вторым независимым recognizer. ElevenLabs Forced Alignment документирован именно для audio/text synchronization и не предоставляет diarization; его adapter используется только в квалифицированном режиме. [ElevenLabs: forced alignment](https://elevenlabs.io/docs/overview/capabilities/forced-alignment).

Provider character alignment служит дополнительной гипотезой: проверяются actual text, counts, ranges, normalized text mapping и drift. При расхождении нельзя распределить таймкоды пропорционально числу слов. Неустановленный range остаётся unresolved для точного transcript/captions; допустимый caption timing error задаётся отдельно по UI qualification. Это не запрет на человеческий ремонт по явно выбранному и подтверждённому диапазону.

Speaker identity проверяется по выбранным voice exemplars, ожидаемому turn order и фактическому звучанию. Diarization label сам по себе не доказывает, что реплику прочитал нужный ведущий. При overlapping speech или неразличимых голосах повторяется сцена/выбирается квалифицированная пара; уверенный label из provider response не подменяет прослушивание.

## 8. AudioQAReport и допуск

```text
AudioQAReport = {
  scope: segment|master, script_ref, performance_plan_ref, transcript_ref,
  audio_segment_ref|null, audio_master_ref|null, policy_ref,
  measurement_records: [{subject_ref, processing_profile_ref, measurement_blob}],
  listening_passes: [{invocation_id, model_profile_ref, audio_ranges[], assessment_blob}],
  human_auditions: [{actor_id, audio_ranges[], assessment_blob}],
  coverage_report: {expected_ranges[], processed_ranges[], uncovered_ranges[], exclusions[]},
  findings: [{finding_id, category, severity:critical|major|minor|advisory,
    blocking, policy_rule_id, sample_ranges[], script_selectors[],
    observation, expected, basis_refs[], resolution:open|confirmed|dismissed,
    rationale, recommended_action}],
  dependency_dispositions: DependencyDisposition[],
  result: acceptable|changes_required|inconclusive
}
```

AudioQAReport хранит оценку точных artifacts; действующий AcceptanceRecord оформляется отдельно. `acceptable` не заменяет atomic eligibility check 04/06. Final report scope=master обязан связывать тот же script_ref с AudioMaster, PerformancePlan и Transcript. Report не редактируется после retake: создаётся новая revision.

`dependency_dispositions` использует общий тип 00; при отсутствии переоценки это пустой массив. Каждый subject_selector — AudioSelector на exact audio_segment_ref для scope=segment либо exact audio_master_ref для scope=master, с границами в PCM этого subject. Ссылка на другой сегмент/master или на сценарий в этом поле отклоняется. Review обосновывает retained/replaced/removed/lineage_only по реально проверенному исполнению, включая соседний контекст; одно совпадение текста или byte hash не заменяет такую оценку.

Application service может применить dispositions через append-only DependencyRevalidationEvent00/06 только после действующего `AcceptanceRecord(check_kind=audio, decision=accepted, subject_ref=exact audio subject)`, чьи reviewed_input_refs включают эту AudioQAReport revision. Event закрепляет exact assessment/authority и expected_validity_version; конфликт требует повторной оценки. Старые edges и provenance сохраняются. Аудиооценка не снимает factual invalidation ScriptRevision, privacy/access revocation и обязательные active bindings к текущему script/plan. Reuse bytes по §9 по-прежнему требует нового exact binding и положенных segment/master/transcript/QA проверок; audio dependency disposition не служит обходом этих требований.

| Проверка | Исполнение и критерий |
|---|---|
| Полнота медиа | Decode всего файла; все planned units есть ровно в нужных местах; actual sample counts и ordering сверены |
| Сохранность речи | Подтверждённые человеком вставка/пропуск/подмена сохраняются как дефекты до решения; ASR mismatch и score сами по себе не блокируют |
| Критические токены | Цифры, отрицания, единицы, имена, даты и цитаты сохраняются в тексте/привязках; сомнение не превращается в автоматический retake |
| Произношение | Словарь и прослушивание конкретного места; безударная редукция/омофония не доказывает ошибку по написанию |
| Голоса | Voice identity, speaker attribution и отсутствие необъяснённого drift на всём выпуске |
| Сигнал | Loudness/true peak/clipping/каналы/decoder integrity; правила привязаны к delivery profile |
| Стыки и ритм | Проверяются все joins и цельные сцены: нет обрезанных слов, двойных вдохов, внезапной смены тембра и случайных пауз |
| Длинное прослушивание | Qualification benchmark естественности/темпа/утомляемости; записывается реально прослушанное покрытие, не предполагаемый полный pass |
| Транскрипт | Words, speakers, boundaries и seek отражают final playback; unresolved intervals отсутствуют |

Прослушивание оценивает реальное аудио, соседний контекст и эффект для слушателя. Для пользовательского локального ремонта решение принимает человек: сначала approve proposal, затем прослушивание исправленного места и обоих стыков. Не требуется предварительный или повторный listening-judge вызов. Если в отдельной qualification применён автоматический assessor, его prompt, clips, profile, ошибки и фактическое покрытие сохраняются по 06; его мнение не подменяет человеческое решение в этом flow.

Технические проверки охватывают весь master, включая начало/конец и все edits. Локальное прослушивание не объявляется полным master listening. Естественность, темп и усталость проверяются на полном человеческом benchmark в qualification 04; обязательного платного автоматического аудиорецензента на каждом выпуске нет. Успех локального ремонта показывает только проверенный участок, его оба перехода и корректность новой версии.

Подтверждённый человеком дефект требует явного решения: исправить звук либо вернуть смысловую правку в 03/04. Непроверенная ASR-гипотеза или автоматический флаг не становятся таким дефектом. Intentional breath/nonverbal event допустим только в рамках performance policy, с записью и без изменения утверждений; неожиданная произнесённая фраза не считается harmless filler. Cosmetic minor finding может быть nonblocking с основанием policy. Severity и blocking не сливаются в одну усреднённую оценку.

Автоматический технический контроль выявляет повреждённый/неполный файл, невозможный формат и измеримые сбои сигнала. Наличие речи, тишина и VAD не устанавливают осмысленность: «ыыыааа» может содержать голос. Детектор такой деградации — отдельная работа с реальными положительными/отрицательными fixtures и измеренными ложными тревогами; до этого обещания его обнаружения и обязательной браковки нет.

## 9. Исправления и повторное использование

Render key хеширует project/scope, ordered spoken text, content-only normalization mapping, speaker/VoiceProfile revisions, model/adapter/profile, lexicon, control tags, normalized settings/seed, prior/future context **фактически переданный provider**, context audio identity, unit boundaries и render implementation version. Content-only mapping содержит исходные/произносимые строки, локальные диапазоны и transformation rules, но исключает script_ref/plan_ref/случайные IDs. Иначе одинаковая речь новой ScriptRevision никогда не могла бы переиспользоваться. Exact script selectors и полный PerformancePlan ref остаются отдельной provenance. Технический request retry и художественный новый take различаются.

Reuse не определяется одним текстом. Если изменился соседний контекст, интонация или voice, прежний сегмент может перестать подходить. Если изменилась только фактологическая опора, а речь и исполнение одинаковы, новый AudioSegment revision может ссылаться на те же bytes с `reuse_basis_ref` и новым exact script/plan binding. Повторное принятие оформляется по новым refs; старый QA не «переименовывается». Runtime dependencies остаются консервативными, пока точность content reuse не проверена.

| Дефект | Ремонт |
|---|---|
| Неверное ударение/произношение | Новая lexicon/profile decision, retake всей достаточной фразы/обмена, соседние joins и QA |
| Подтверждённый пропуск, повтор, лишняя фраза | Proposal на одно-два предложения, approve exact range/text, retake с тем же voice/settings и проверкой обоих joins |
| Плохой стык | Расширить unit до устойчивой границы/соседнего обмена; не накрывать ошибку тишиной |
| Верный текст, неверная убедительность/сарказм | Изменить performance direction; при изменении смысла — 03/04 |
| Длительность за допуском | Редакционное решение о структуре/подаче; новая ScriptRevision при изменении слов |
| Неверная исходная формулировка | 02/03 исправляют содержание, 04 перепроверяет, затем новый план/звук |
| Недоступный voice/provider | Явная ошибка или заранее квалифицированный replacement; новый voice binding требует consistency review всего ведущего |

После локальной замены создаются новый immutable master и карта изменения времени. Таймкоды до диапазона сохраняются, после — сдвигаются на измеренную delta; пересекающие замену captions/chapters/word alignment пересчитываются либо явно остаются unresolved. Старый transcript/QA не переименовывается в новый. Повторный ASR всего выпуска и автоматический reviewer не являются предварительным условием замены; короткий ASR сгенерированного контекста определяет границы середины. Корректность вставки и устранение отмеченного места проверяются прослушиванием по §16. Release/ShowNotes связываются с новой точной версией по 04/06.

## 10. Исполнение, ошибки и остановка

Operational stages: `planning → rendering → checking_segments → assembling → transcribing → checking_master → complete`; это audio job projection в06, не отдельная конкурирующая EpisodeRun machine. `complete` означает сохранённый audio результат; ready определяется комплектом 04/06. Failed/cancelled/needs_attention и checkpoints видны пользователю без фиктивного прогресса.

Локальный пользовательский flow отдельно показывает `proposal → approved → contextual TTS → ASR localization → extraction → local_master` с `listening_status=not_reviewed`; повторный ASR всего выпуска и perceptual reviewer не требуются. Принятие прослушанного результата и release eligibility записываются отдельно от завершения сборки.

| Событие | Реакция |
|---|---|
| Timeout/429/5xx | Runtime retry/reconciliation; тот же logical operation, без дублирующей adoption |
| Потерян ответ после submit | outcome_unknown, retrieve если поддержано; replacement invocation с lineage, late reply fenced |
| Partial stream/повреждённый codec | Diagnostic bytes сохраняются, сегмент не допущен; повтор от неизменных входов |
| Unsupported language/tag/dictionary | Typed failure до запуска/после capability detection; не игнорировать параметр |
| Локальный дубль не подходит | Сохранить результат; человек прослушивает место/стыки и выбирает новый take либо revised proposal. Диапазон/текст/voice не менять под старым approval |
| Cancel во время FFmpeg/TTS | Остановить subprocess/cancel provider где возможно; после epoch change late output не создаёт release |
| Source/script approval отозван | Остановить adoption, сохранить диагностический прогресс, вернуть задачу редактору |

Начальный предел — три quality retakes одной unit с неизменным creative intent; далее причина разбирается отдельно, меняется профиль/граница или возвращается needs_attention. Это защита от бесконечного цикла, не разрешение принять третий плохой take. Transport retries задаёт06. Успех декодирования/ASR не превращает TaskResult partial в answered по качеству.

## 11. Контекст, инструменты и примеры

Режиссёр получает EpisodeSpec, exact verified script целиком либо полный акт с глобальным планом и соседними сценами, speaker profiles/аудиопримеры, pronouncing lexicon, actual prior take когда релевантно и supported capability contract. Контекст не заменяется списком названий актов. Большой текст сохраняется полностью; model-facing view/пагинация явные.

| Tool | Содержательный вход | Результат |
|---|---|---|
| `read_performance_material` | script/scene/voice alias, selector, detail | Полный текст, направление, exact profile + safe qualification projection, только разрешённые published/project-local audition и context audio |
| `save_performance_plan` | scene directions, bindings, pronunciations, boundaries | Полный plan или точные validation issues; backend IDs/refs |
| `request_audio_repair` | exact audio version, примерное время, пользовательское описание | Proposal с текстом/диапазоном/контекстным preview; без TTS до exact approval; semantic changes маршрутизируются редактору |

Tools используют `ToolResult/ToolError` из06. Ошибки: `stale_revision`, `unqualified_voice`, `unsupported_audio_capability`, `normalization_requires_editorial_change`, `render_capacity_exceeded`, `incomplete_audio`, `unresolved_audio_discrepancy`, `invalid_audio_range`. Модель не запускает FFmpeg command string и не выбирает filesystem path; media commands строит typed application layer.

```text
DIRECTOR: Perform the verified script so the listener understands the transition.
Preserve words, qualifications and speaker roles. Choose direction and exchange
boundaries within the capability profile. Flag unresolved pronunciation; return
semantic changes to the editor. Save the plan with exact script selectors.
Use output_language from the user payload for textual output fields.
```

Учебный пример: в сценарии `u17` написано «Тест длился 12 недель, не 20». Plan сохраняет spoken form «Тест длился двенадцать недель, не двадцать» с mapping обоих чисел и неизменным отрицанием. Если человек услышал «двадцать недель», агрегатный WER может быть мал, но это подтверждённый дефект для локального ремонта. Forced aligner, успешно расставивший времена для ожидаемого текста, не снимает finding. Retake должен реально произнести правильное число; новый final master/transcript/QA связываются exact refs.

Второй пример: одинокая реплика вопроса звучит завершённым монологом, ответ другого ведущего стартует с чужим темпом. Вместо искусственного короткого gap синтезируется весь обмен в одном допустимом contextual unit; после монтажа проверяются оба перехода и естественность сцены. Это экспериментально проверяемый способ ремонта, не гарантия конкретного provider.

## 12. Хранение, безопасность и пакеты

PostgreSQL хранит общие artifacts/dependencies, render intents, take selections, alignment indexes и job projections. Object storage — provider originals, PCM, masters, deliveries, raw ASR, alignment maps, measurement reports и model payloads. Проверяемые bytes и полные semantic fields не заменяются wave preview/summary. Delete/retention по06 охватывает voice exemplars, audio/transcript, exports, provider stored requests и cached context; private pronunciation lexicon не отправляется в неразрешённый сервис.

Media workers изолированы, используют vetted decoder builds, CPU/memory/decompression/time limits и cancellation heartbeats. Внешний файл не даёт сетевых прав FFmpeg. Credentials/подписанные URL скрыты от модели; args передаются массивом, не произвольной shell-строкой. Exact source/script text сохраняет trust label; tool permissions не меняются из услышанных инструкций.

| Пакет `backend/src/podgen/audio/` | Ответственность |
|---|---|
| `domain/` | Profiles, plan, mappings, segments/master/transcript/QA types |
| `performance/` | Scene direction, lexicon, spoken normalization, plan compilation |
| `synthesis/` | Provider adapters/capabilities, request segmentation, render/take/reuse |
| `assembly/` | Typed edit list, PCM processing, normalization, encoding, playback maps |
| `alignment/` | Independent recognition, discrepancy mapping, timestamp alignment |
| `quality/` | Signal/fidelity/voice/listening checks, repair proposals, coverage |
| `application/` | Audio production/recovery use cases, runtime ports, exact-version handoff |
| `tools/` | Model-facing tools and profile-bound prompts; infrastructure hidden |

Model/task/cancel/outbox общие с06; audio не создаёт второй agent framework. AudioQA не выполняет повторное исследование самостоятельно: фактическая проблема возвращается через редакционную задачу04/02.

## 13. Приёмка и квалификация

Обязательные интеграционные сценарии: оборванный stream с HTTP200; ошибка одного segment; неверное число/отрицание/имя; пропуск концовки; дубликат unit; speaker swap; ошибочная provider normalization; voiced stage direction; вырезанное VAD тихое слово; cut на границе; длинный drift timestamps; changed neighbor context; script revision во время TTS; cancel и поздний callback; lossless/master→MP3 delay; source deletion и reuse старого private segment. Каждый должен иметь наблюдаемый отказ/repair и exact-version lineage, а не просто log warning.

Qualification corpus содержит реальные RU/EN сценарии одного/двух ведущих, смешанные имена/аббревиатуры, числа/даты/единицы/отрицания, эмоциональные изменения, короткие ответы и длинные объяснения. Для языка/формата, заявленного продуктом, нужны короткие clips и целые 15/30/60/90-минутные выпуски. Поддержка 90+ минут добавляется через отдельные случаи и длительное прослушивание; масштабирование числа запросов само её не доказывает.

Слепое человеческое сравнение вариантов выравнивает playback loudness, рандомизирует порядок и использует одинаковый текст. Оцениваются понятность, естественность разговора, произношение, усталость, стыки и желание слушать дальше. Отдельные annotators размечают пропуски/вставки/неправильные слова, speaker labels и word boundaries. EvaluationResult из04 сохраняет case/profile/refs/ratings, не только усреднённый MOS. Среднее не скрывает критический defect одного выпуска.

Автоматические метрики калибруются по human annotations: fidelity defect recall/false alarms, критические токены, boundary error, speaker confusion, full waveform coverage, loudness/peak, repair success. Не задаётся универсальный WER/ASR confidence как разрешение на выпуск. Точное отсутствие неразрешённых fidelity findings и валидные refs — структурные условия; естественность/понятность оцениваются независимо. Полный held-out episode benchmark из04 остаётся финальной проверкой.

Реализация по порядку: (1) plan/mapping/profile contracts и golden Unicode/normalization fixtures; (2) provider adapter+fault harness и качественный контекстный рендер; (3) PCM montage/delivery с измеряемыми maps; (4) сохранённое recognition/alignment для локализации; (5) человеческий timestamp → approved local repair → прослушивание места и двух joins; (6) exact release/transcript/show-notes integration и длинные qualification cases. Завершение модуля — полноценный проверенный выпуск с естественным звуком и воспроизводимой связью текста/аудио, а не первая успешно синтезированная реплика.

## 14. Реализованный путь первичного сравнения голосов

`audio-comparison/1` — evaluation-путь отбора, а не производство AudioMaster/Transcript/AudioQAReport. `python -m podgen.audio_evaluation_cli` использует существующие evaluator-only runtime records, blob storage и ACL; новые таблицы и подмена ScriptRevision не нужны. Проверяются native `independent-reading-outcome/1`, общий script hash и обе финальные рецензии `keep`. Вход сохраняет exact runtime refs, стабильные UUID реплик и исходные char ranges. Выбранные слова не переписываются.

Конфигурация описана в [comparison.schema.json](audio/comparison.schema.json), голос — в [voice-candidate.schema.json](audio/voice-candidate.schema.json). `providers` — разрешённая часть матрицы; `profiles` содержит только реально выбранные голоса. Полная матрица — пять провайдеров × два голоса × два фрагмента. Отсутствующие профили/ключи явно остаются `not_configured`; четыре записи одного сервиса не объявляются результатом сравнения пяти. Базовая `direction.style` пустая. SparseDirection относится к execution input; прежний canonical Direction и сохранённые PerformancePlan не изменены. Преобразование canonical VoiceProfile/ModelProfile в тот же synthesis port само по себе не даёт production acceptance.

```sh
python -m podgen.audio_evaluation_cli --project PROJECT --actor ACTOR \
  --objects OBJECTS --env-file .env prepare --config comparison.json --export-dir comparison
```

Те же аргументы используются с `run`, `resume`, `export`. Только `run`/`resume` и явный `retake --sample-id CELL_UUID --reason TEXT` могут отправлять синтез. Системных retries, покупок тарифа, скрытой смены модели или голоса нет. Submit intent без полного receipt остаётся `outcome_unknown` и резервирует стоимость. Rate/quota refusal сохраняется без автоматического повтора. Все платные попытки учитываются в `max_attempts` и `max_cost_usd`; неподтверждённые расходы остаются reservations. Оценка по опубликованной цене не называется invoice: actual usage и raw billing headers сохранены отдельно. Reservations должны покрывать консервативную оценку одного запроса; фактическое превышение не скрывается и блокирует следующие отправки.

`reprocess --sample-id CELL_UUID --reason TEXT` создаёт новую immutable обработку полного сохранённого успешного HTTP response. Ключ и transport ему не нужны; исходная ошибка/обработка остаются в истории. Это отличается от художественного retake. Первый технически пригодный synthesis take остаётся baseline; последующие художественные takes не заменяют его в основном blind-наборе. Внутри baseline используется последняя успешная обработка его receipt; ошибка более новой обработки остаётся отдельно видна в истории и статусе, не стирая пригодный baseline.

`private/` содержит config, source records с точным текстом, подготовленные запросы и фактические UTF-8 wire bodies без секретных auth headers, raw HTTP responses, receipts, исходные provider audio, decoded PCM, все обработки и расходы. `blind/` содержит обезличенные WAV, случайный сохранённый порядок и страницу прослушивания с закрытым текстом для второго прохода. Заметки изолированы по configuration hash. Native WAV/PCM/FLAC сохраняются без lossy-перекодирования; MP3 под видом lossless отклоняется. Полное декодирование измеряет frames/rate/channels/LUFS/true peak и `full_scale_samples` по исходной bit depth; это последнее поле само по себе не доказывает слышимый клиппинг.

Каталог comparison-экспорта до первой записи привязывается через `export-binding.json` к run_id, exact manifest ref и hash полного manifest. Listening-экспорт использует свою `batch.json` с exact batch ref/hash. Непустой каталог без подтверждённой привязки отклоняется без изменений; старый comparison-экспорт допускается только при точном совпадении `private/manifest.json` и отсутствии чужих cell/source/sample records в служебных namespaces. Имена и содержимое sources, labels и hash WAV сверяются с сохранёнными records. Пользовательские заметки и подпапки `blind/` сохраняются; чужие служебные остатки приводят к отказу без удаления. `run`/`resume`/`retake` проверяют и привязывают каталог до создания transport и до оплаты.

Blind-копии приводятся к общим mono/48 kHz/PCM24 и loudness-target постоянным измеренным gain, без изменения темпа и скрытой компрессии. Если target нельзя получить без превышения true peak, обработка явно останавливается. Провайдерские исходники остаются нетронутыми. Корректный сигнал и HTTP200 не удостоверяют исполнение слов, произношение или естественность; финалистов выбирает прослушивание. Независимые ASR, acoustic review, full-master assembly и локальные production retakes остаются следующими частями WP-07, не заменяются заглушками.

### Отдельные локальные издания для прослушивания

`listen --batch-id UUID` создаёт immutable `audio-listening-batch/1` поверх сохранённых decoded records, включая записи с прежним отказом нормализации. HTTP-запросов и ключей не требует. Frozen comparison config, requests и profiles не меняются. Используется первый пригодный synthesis take и последняя пригодная decoded-обработка этого take.

Сначала каждый сигнал преобразуется в mono/48 kHz/PCM32 и измеряется. Общая цель — минимум из `--preferred-lufs` (по умолчанию −20) и допустимых целей всех сигналов с ограничением `--true-peak-dbtp` (−1) и `--headroom-db` (0.3); округление вниз до 0.1 LU. Затем применяется только постоянный gain с выходом PCM24. Ограничение пиков не заменяется компрессией. Manifest сохраняет точные decoded/input refs, измерения, версии инструментов и общий target; прежние ошибки остаются отдельно от новых результатов.

Явный `--repair-observed-fish-header` разрешает отдельную контейнерную копию только наблюдённого Fish WAV: 44-byte header, PCM16 mono, согласованные rate/byte-rate/block-align, sentinel `RIFF=0xffffff24`, `data=0xffffff00`, полный HTTP200 и целые frames. Меняются только uint32 length по offsets 4 и 40; исходник, repaired blob, hash исходника/результата/неизменного PCM payload, оба patches и receipt ref сохраняются. Остальные повреждённые WAV отклоняются; строгий `-xerror` decode сохранён. Repair не доказывает полноту произнесённых слов: fidelity остаётся `pending_listening`.

```sh
python -m podgen.audio_evaluation_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  listen --config comparison.json --export-dir listening/UUID --batch-id UUID \
  --repair-observed-fish-header
```

`listen-export` с теми же config/batch-id экспортирует сохранённое издание. `batch.json` привязывает каталог к точному batch ref/hash; другой batch туда не записывается. `private/` содержит manifest, соответствие меток, decoded provenance, repair audit и результаты; `blind/` — обезличенные файлы и страницу с отдельным пространством заметок. Coverage различает expected/configured, полные HTTP200, provider refusals и реально доступные listening samples. Успех локальной команды означает готовность этого издания, а не полноту матрицы провайдеров или production acceptance.

### Целевая проверка полного сценария

`audio-comparison/2` ([schema](audio/comparison-v2.schema.json)) допускает один или два настоящих фрагмента и явно задаёт `expected_voices_per_provider`, например `{"gemini": 1}`. Ключи должны точно совпадать с `providers`; ожидается один или два голоса на провайдера. Число профилей не может превышать заявленное; недостающие профили остаются видны как неполный набор. Expected samples = сумма ожидаемых голосов × число фрагментов. Полный сценарий задаётся одним фрагментом с selections всех его реплик; source refs, exact text и обе финальные `keep` проверяются прежним путём.

`request_timeout_seconds` задаёт HTTPX timeout для сетевых операций (включая ожидание очередного блока ответа), от 1 до 1800 секунд; default 180. Это не ограничение длительности речи и не обещание общего wall-clock deadline. Timeout сохраняет неполный receipt как `outcome_unknown`, без автоматического retry. Изменение timeout или ожидаемого набора меняет configuration hash и требует нового run_id.

`audio-comparison/1` сохраняет прежний payload/hash без новых default-полей: ровно два фрагмента, два ожидаемых голоса на провайдера, HTTP timeout 180 секунд. Пять провайдеров по-прежнему означают expected=20. Run/export/listening используют ожидаемую матрицу исходного manifest; успешная проверка 1×1 не называется полным сравнением пяти провайдеров. CLI-команды для обеих версий одинаковы.

## 15. Evaluation: независимый ASR и кандидаты fidelity

`python -m podgen.audio_fidelity_cli` реализует первые два этапа: независимое распознавание сохранённого результата и подготовку кандидатов расхождений с аудиоклипами. Это не perceptual adjudication, final Transcript или production acceptance. Результат всегда сохраняет `audio_review_status=pending`, даже если текст совпал.

Конфигурация [audio-fidelity-evaluation/1](audio/fidelity-evaluation.schema.json) задаёт audit_id, comparison_run_id, exact result_ref, recognition profile, цену с источником/датой, reservation/max_cost и размер review-окон. Принимается только зарегистрированный успешный `audio-sample-result/1` выбранного comparison; source/script refs/hash остаются исходными. Текущий профиль — один рассказчик, Fish `transcribe-1-pro`, language=ru, timestamps включены, diarize/tag_audio_events выключены. Передаётся только звук; сценарий, hints и prompt отсутствуют. [Официальный Fish ASR contract](https://docs.fish.audio/features/speech-to-text.md).

Проверяется **decoded PCM до playback gain**, а не нормализованный файл проигрывателя. Manifest и отчёт явно сохраняют этот вариант, exact hash/decoded ref, связь с playback blob и его normalization. В ASR идёт FLAC-копия без VAD/обрезки/изменения темпа: весь вход и FLAC полностью декодируются в PCM32 и побитно сравниваются, проверяются rate/channels/frames. Несовпадение блокирует отправку. Хранятся оба file hash, оба PCM hash и полное submitted frame range.

Manifest сохраняет config/hash, exact source/result/decoded refs, FLAC, фактический multipart body, явный model header, версии adapter/processing, отсутствие prompt и полные schema snapshots как отдельные blobs. Submit intent и reservation записываются до сети; полный raw HTTP receipt — до JSON parsing. Subject закреплён за одним audit отдельным claim: новый audit_id не обходит ограничение. Resume не создаёт новую оплачиваемую попытку, unknown не пересылается. Отказ провайдера остаётся `provider_rejected`, неполная доставка/потеря acknowledgement — `outcome_unknown`; raw body/error/headers не теряются. Budget проверяется до отправки; reservation обязана покрывать оценку полной длительности по frozen price. Расходы остаются estimate/reservation, не invoice. Покупок, hidden retries или смены модели нет.

ASR `text` и все `segments` сохраняются дословно, вместе с исходным response. Сопоставление [literal-fidelity-candidates/5](audio/fidelity-candidates-v5.schema.json) использует базовый literal слой регистра, пунктуации и ё/е плюс отдельное ограниченное числовое сопоставление, описанное ниже. Числа/десятичные разделители/числовые знаки, отрицания и формы слов не стираются; стемминга и семантических исправлений нет. Кандидат содержит исходные tokens, индексы/char ranges, selectors, соседний исходный контекст и гипотезу frame range с margin. Совпадения ASR text с segments дают только гипотезу времени. Несовпадение текста/segments, плохой timeline или duration mismatch — отдельные uncertainty candidates. Пропорционального распределения времени нет.

Candidates/5 использует `kaldialign==0.12.0` для edit alignment script↔ASR поверх прежних числовых comparison units. `merge_compounds` разрешает только буквенные literal units; цифры, числовые units и символы получают несклеиваемые метки. `по тому`↔`потому` — явное правило записи `literal-alphabetic-compound-equivalence/1`, не доказанное совпадение произношения. Raw tokens, char offsets, utterance selectors и полные ranges сохраняются; compound opcode — `equivalent`, не 1:1 `equal`. Report и processing revision фиксируют engine/version/options и оба правила equivalence. Transcript↔segments timing не меняется. [Старые schemas /1–4](audio/fidelity-candidates.schema.json), manifest/wire/results остаются прежними; только явный локальный `reprocess` создаёт новую revision без API.

Слой normalization/3 сохраняет математические/валютные/единичные символы (включая ±, <, >, %) как смысловые tokens. Нулевой timestamp остаётся в raw ASR и diagnostics, не используется как надёжный anchor и не создаёт отдельный audio case при совпадающих словах. Диагностические candidates создаются для source/transcript и transcript/segments различий; проблемы локализации прикрепляются к затронутому case. Segment без equal-match связывается с полным соответствующим mismatch-opcode без пропорционального времени. Для всех uncertainty cases через текстовый alignment сохраняются expected tokens/selectors и ближайшие полные исходные реплики; это не акустическое подтверждение. Case IDs определяются содержимым кандидата, а не его порядком. Старые results/schemas/IDs не переписываются; новый локальный `reprocess` явно фиксирует новую версию и schemas.

Нормализация `/3` сравнивает однозначные целые числовые выражения отдельными units поверх неизменных raw tokens. Поддерживается явная грамматика cardinal counts, согласованные перечисленные родительные формы и календарный день перед названием месяца; source/recognized spans и принятые equivalences сохраняются полностью. Не используются stemming/fuzzy matching или бесконтекстная эквивалентность всех чисел. Пунктуация/переносы между числами не склеивают список; ведущие нули, дроби, знаки, единицы, отрицания и неоднозначные/неполные формы остаются видимыми. `сто ноль`, `двадцать ноль`, ноль после scale, `миллион тысяча` не превращаются в число. Текстовый alignment equivalent span не распределяет акустическое время пропорционально словам; исходные ASR segments/диагностика не переписываются.

Клип вырезается по точным PCM frames без изменения payload; сохраняются frame range/hash и исходная привязка. Окно длиннее max_review_clip_seconds (не более 45 секунд), в том числе полностью немаппящийся длинный участок, остаётся `inconclusive` без фиктивного короткого клипа. Совпадение текста и нулевой diagnostic count не доказывают правильность звука. Этот CLI даёт диагностику и локализацию, не обязательную браковку или разрешение на ремонт; он не исправляет сценарий и не отправляет TTS.

```sh
python -m podgen.audio_fidelity_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  --env-file .env prepare --config fidelity.json --export-dir fidelity-export
```

`run`/`resume` используют те же аргументы. `retry --reason TEXT --max-total-requests N --max-total-cost-usd USD` создаёт отдельную immutable retry authorization с parent receipt и общим лимитом 2–3 отправки. Допускается только полный provider rejection, не unknown и не уже успешный HTTP200. Старые configs с `max_requests=1`, manifest/hash и wire не меняются. Каждая дополнительная доставка имеет собственные intent/receipt; reservations всех отправок входят в общий budget. Resume может завершить уже авторизованную попытку, но новую authorization не создаёт. Ключ и body остаются прежними. Только `run`/`resume` и явный `retry` могут отправлять ASR-запрос. `reprocess --reason TEXT` создаёт новую immutable обработку уже полного успешного receipt без ключа/сети. Старые результаты и ошибки сохраняются. `export` выгружает manifest, schemas, точный wire body, FLAC, raw receipt, все обработки и клипы; каталог привязывается до записи и до возможной оплаты. `candidates_generated` означает завершение этого этапа, а не подтверждение качества речи.

Экспорт fidelity различает current processing и last successful result. Полные результаты/клипы лежат в immutable `results/<result-id>/`; `current.json`, `candidates.json` и status явно указывают актуальную revision. После failed reprocess или новой неуспешной доставки current report равен null, а предыдущий успешный результат доступен только как last successful с exact ref/path. Старые файлы остаются в истории; новый receipt не получает чужой старый результат. Каждый immutable JSON/WAV атомарно публикуется через temp+rename и независимо verify/create при resume; JSON не считается признаком полноты каталога. Валидные файлы не переписываются, изменённые вызывают conflict.

## 16. Contextual local replacement по указанию человека

### Заменяемый текст, озвучиваемый контекст и approval

Вход — exact immutable audio version/ref/hash, примерное время и пользовательское описание. `prepare` только находит место по сохранённой карте и предлагает **одно-два целых предложения для замены** с точным `[start_frame,end_frame)` исходного master. Исходные слова/числа не переписываются. Before-preview показывает этот диапазон и соседнюю оригинальную речь; неуверенная локализация явная, выбор предложений можно сдвинуть/расширить до approval.

Новые proposals используют contextual `/2`. Они различают:

- **replacement text** — только выбранные предложения, которые изменятся в master;
- **synthesis text** — replacement плюс одно предыдущее и одно следующее предложение с доступных сторон, в исходном порядке; это явно **произносимый** контекст для TTS, а не инструкция модели;
- selectors и положение replacement внутри synthesis text, exact original range/rate, parent ref/hash и source lineage;
- те же voice/model/style/settings и speaker bindings; точный TTS request и ASR profile/options для последующей локализации;
- before-preview range/hash, proposal id/revision/hash. Лимиты/резервы TTS и ASR сохраняются в approval раздельно.

На начале/конце выпуска включаются только доступные соседи; состав контекста виден в source/sentence/target ranges и context policy. Отдельного поля причины отсутствия соседа сейчас нет. Соседа не сочиняют и не заменяют повтором target. Смена speaker требует сохранённых подходящих voice bindings для каждого озвучиваемого предложения. Нельзя читать чужую реплику голосом target или молча убрать доступного соседа. Неподдержанное сочетание — явная несовместимость до оплаты. Capacity считается по **всему** synthesis text; лишний текст не обрезается.

**Одно человеческое подтверждение** exact proposal hash разрешает последовательность TTS + необходимый ASR с двумя явно заданными бюджетами. Второй approval на штатный ASR не нужен. Пользователь видит, что заменяется, что дополнительно озвучивается и зачем. Approval сохраняет actor, дату, identity proposal и лимиты обоих этапов. До него не выполняются ни новый TTS, ни ASR сгенерированного контекста. Изменение target, контекста, диапазона, profile или request revision требует нового proposal/hash и approval. ASR/TonePerfect/LLM verdict качества не является разрешением.

Старые frozen `/1` proposal, approval, request и результаты сохраняются дословно; contextual request нельзя подставить под прежнее подтверждение. Для нового contextual запуска нужен отдельный `/2` proposal. Свежие prepare/revise не создают target-only `/1`. Это versioned переход, не молчаливая смена approved body.

### TTS, локализация середины и восстановление

До submit проверяются exact approval, parent ref/hash и bytes, неизменность request и бюджет соответствующего этапа. Один TTS озвучивает полный synthesis text тем же профилем. Его полные receipt/provider audio/decoded PCM сохраняются **до** ASR, независимо от успеха локализации.

ASR получает только новое generated-context audio с frozen profile/options; он не получает ожидаемый script/hints. Полный raw transcript/segments и exact audio subject/hash сохраняются. Text alignment нужен только для поиска replacement в этой записи. Это не listening judge и не доказательство правильного произношения.

Вырезать середину можно только при однозначном соответствии target и граничных anchors/timestamps, с проверкой order/ranges/ненулевых spans. Пауза уточняет границу внутри установленного межсловного промежутка; minimum energy не доказывает смысл. Повторяющийся текст, отсутствующие anchors, overlap или неоднозначное соответствие не разрешают линейную интерполяцию либо выбор первого похожего span. Слова с нулевой длительностью и общим началом следующего ненулевого сегмента объединяются в наблюдаемую timing-группу: допустима её внешняя граница, но внутренних таймингов слов нет. Граница внутри группы или без ненулевого межсловного зазора отклоняется. При отказе сохраняется TTS и localization failure, без splice.

TTS и ASR имеют собственные intents, receipts, reservations и checkpoints под **одним** approval. Timeout/429/invalid ASR и ошибка выбора boundaries не запускают TTS повторно. Resume продолжает незавершённый этап, reprocess выбранного этапа перечитывает сохранённый receipt без сети. Явный retry локализации использует уже сохранённый generated context и свой approved budget; unknown не пересылается молча. Новый художественный take — отдельное действие, не автоматический ответ на ASR mismatch.

### Exact splice, timeline и цельное прослушивание

Сохраняются весь generated context, выбранные в нём frames, extraction proof/hash и отдельный middle PCM. Только эта середина вставляется в утверждённый **исходный** range: `old[0:start] + middle + old[end:N]`. Новые озвученные соседние предложения не заменяют оригинальное окружение. При необходимости конвертируется/выравнивается по исходной signal policy только middle; PCM вне approved edits побайтно прежний. **Коррекции темпа нет:** не ускорять/растягивать фрагмент ради совпадения длительности; tempo-проба не является продуктовым режимом.

Перед apply повторно проверяется тот же parent ref/hash и source blob. Явная новая ветка от старого immutable master допустима; глобальный latest pointer не требуется. Master/lineage публикуются атомарно. `delta_frames = middle_frames − (end − start)`; suffix сдвигается на измеренную delta, intersecting captions/chapters/timing пересчитываются либо остаются unresolved. Ни старые timestamps, ни submitted text не превращаются автоматически в новый фактический transcript.

Несколько approved proposals одного parent можно применить совместно: non-overlapping ranges, все approvals и локализованные middles проверены; одна новая версия, накопленная сумма delta. Неполный набор не принимается как полная операция. Source original, старые proposals/masters и raw outputs не меняются.

**Основной результат прослушивания — один цельный after-preview на каждую замену:** непрерывный диапазон **из нового master**, содержащий middle и оба перехода в реальную оригинальную речь вне approved edits. Он не собирается из generated-context neighbours и не заменяется двумя отдельными start/end clips. Before/after имеют явные version/range/hash. Для края записи preview содержит доступное окружение и реальный край, без фиктивного соседа. Полный master и diagnostic join clips могут сохраняться дополнительно.

Человек слушает исправленные слова, темп/подачу и оба перехода **в одном after-preview**: нет обрезанных слов/вдохов, повторов, щелчков, странных пауз, скачка тембра/громкости. Contextual generation и одинаковый голос не гарантируют хорошее звучание. Текущий CLI не записывает human audition verdict, master остаётся `listening_status=not_reviewed`, `production_ready=false`; будущий release acceptance хранится отдельно. Механическая целостность и успешный ASR не означают acoustic pass.

Legacy listening-judge/TonePerfect эксперименты и автоматический детектор бессмыслицы не являются шагами этого ремонта; signal checks описаны в §8.

## 17. Evaluation: сборка полного listening master

`python -m podgen.audio_assembly_cli` выполняет только локальную сборку сохранённых synthesis results. [Assembly config](audio/assembly.schema.json) задаёт comparison_run_id, ordered part_result_refs, assembly_id, preferred loudness/true peak/headroom и размер join preview. Ни synthesis, ни ASR/reviewer transport этому CLI не нужны.

До обработки проверяются членство каждого result в выбранном comparison, точное совпадение frozen профиля/style/script ref/hash/outcome и native финальных `keep`. Конкатенация ordered selectors и исходных utterances должна точно равняться всему проверенному сценарию: нельзя пропустить, повторить, переставить или частично скрыть реплику. Это проверка source coverage, не заявление о фактически услышанных словах.

Собираются исходные decoded PCM, без прежних индивидуальных playback gains. Проверяются формат и целые frames; полный PCM каждого входа побитно сохраняется в промежуточной concatenation. Нет cuts/crossfades/вставленной тишины/изменения темпа. Итоговая normalization применяется один раз ко всему assembled PCM, с измерением после mono/48k conversion. При необходимости единый target понижается с заданным запасом true peak; requested/effective target и один applied gain сохраняются.

`audio-evaluation-assembly-result/1` — отдельный immutable runtime record. Он содержит промежуточный PCM до gain, итоговый PCM48/PCM24 master/player, полный decode/metrics, source→final master frame map, profile/source bindings и joins. Карта сохраняет source sample rate/ranges, промежуточные assembly ranges и final master ranges, measured конечный endpoint и правило округления resampling; последовательные master ranges покрывают всю запись без дыр. Для 24k→48k границы имеют точный коэффициент 2. Join preview извлекается из **итогового master** с тем же общим gain и exact frame extraction; его acoustic review остаётся pending. Старые источники/masters не меняются.

```sh
python -m podgen.audio_assembly_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  build --config assembly.json --export-dir assembly-export
```

`build` идемпотентен для frozen assembly_id/config, `export` восстанавливает файлы из сохранённых blobs. `master.wav` и player — конечный сигнал; `private/assembly-pcm-before-gain.wav` — промежуточная PCM concatenation. `frame-map.json` и `status.json` сохраняют измеренную связь источников с master. Каталог привязан к exact assembly ref/manifest hash. Acoustics и spoken fidelity не выводятся из корректности сборки: `acoustic_verdict=pending`, `production_ready=false`.

ASR [audio-fidelity-evaluation/2](audio/fidelity-evaluation-v2.schema.json) имеет явный `subject={kind:assembly,ref:...}`; `result_ref` comparison ему не подделывается. Resolver проверяет зарегистрированный assembly manifest/result, членство, source/hash и full source coverage. В ASR идёт **итоговый master PCM48 после gain/resample**, тот же hash, что у файла проигрывателя; FLAC transport вновь побитно сверяется с ним. `audio_variant=assembled_final_playback_pcm`, `decoded_ref=null` и настоящий assembly subject ref явно сохраняются. Source/full script не режется. Существующий fidelity `/1` и его config/hash остаются прежними: Apollo проверял decoded PCM до gain, что честно отражено в его сохранённых records.

## 18. Приёмка contextual replacement и сохранённые случаи Snow

Пока UI нет, используется `python -m podgen.audio_local_replacement_cli` на `AudioJournal`, runtime `audio/local_replacement`: подготовить/revise proposal → approve exact hash → contextual TTS → ASR/localization/extract → apply/export новой версии → один цельный after-preview с replacement и обеими сторонами splice. Исторические assembly/repair-part bindings разрешают чтение прежних источников, а не запуск снятого whole-request repair. Новый `local_master` subject адресует конкретную производную версию. Команды разделяют подготовку, один пользовательский approval, TTS, ASR/localization, extraction и сборку. Принятие качества/будущий release — отдельный контракт: CLI его пока не записывает и не объявляет; успешный decode не заменяет audition.

Общие CLI args: `--project --actor --run-id --objects`, при submit — `--env-file`; у каждой команды свой `--export-dir`. `prepare --config` принимает contextual `/2` с `localization` profile/price и сохраняет `proposal.json`, replacement/synthesis text, `selected.wav`, `context.wav`, exact hash и frozen TTS request; сеть не вызывается. `revise --proposal-id` создаёт новый `/2` proposal ID, поддерживает `--shift-sentences`, `--sentence-count`, `--approximate-seconds`, `--new-proposal-id` и `--localization-config`. При переходе со старого `/1` localization config обязателен.

`approve` принимает `--proposal-hash`, `--reason`, TTS-лимиты `--reservation-usd --max-cost-usd --max-requests` и ASR-лимиты `--asr-reservation-usd --asr-max-cost-usd --asr-max-requests`; сети нет. `synthesize`/`resume` выполняет approved TTS и затем ASR/localization; сохранённый успешный TTS повторно не отправляется. `retry --stage tts|asr --reason` разрешён для известного provider rejection в соответствующем budget. `reprocess --stage tts|asr --reason` обрабатывает сохранённый успешный receipt выбранного этапа без сети; после TTS-reprocess ASR запускается отдельным `resume`. Для ASR retry/reprocess TTS не вызывается; default stage — `tts`.

`apply` принимает один или несколько повторённых `--proposal-id`; optional `--parent-subject` проверяет явно ожидаемую базу. Экспорт новой версии содержит `master.wav`, `timeline.json`, `result.json` с edits/frame map и цельный `replacement-N-context.wav` каждой замены; отдельные join clips — дополнительная диагностика. `export`/`export-master` восстанавливают assets по immutable proposal/master refs, не генерируют звук повторно. ASR input/wire, raw receipts, extraction history и `middle.wav` сохраняются в `private/<context-audio-sha256>/` proposal export.

| Сценарий | Проверяемый результат |
|---|---|
| Есть только timestamp/proposal, approval отсутствует | Ноль новых TTS/ASR submits; показаны replacement, synthesis context и два бюджета |
| После approval изменены range/text/profile/context или request revision | Новый `/2` proposal и hash; старый approval не разрешает ни TTS, ни ASR нового request |
| До submit/apply передан другой parent/hash либо испорчен исходный blob | Нет оплаты/вклейки на неверной базе; late response сохранён. Явная ветка от прежнего immutable master допустима |
| TTS успешен, ASR timeout/invalid/ambiguous; повторный resume | TTS сохранён и не отправлен снова; отдельные ASR intent/budget/checkpoint; при неопределённых boundaries splice отсутствует |
| Replacement короче/длиннее исходного | Точная delta/map/новая duration; suffix сохранён и сдвинут, intersecting annotations пересмотрены |
| Два proposals на одном parent применяются совместно | Non-overlapping ranges, approvals и extracted middles проверены; одна новая версия и два цельных after previews, без частичной adoption |
| Новая версия создана | Старый master доступен побайтно прежним; новая версия имеет собственный ref/hash |
| Цельный after-preview | Exact contiguous PCM нового master с middle и исходными соседями; оба перехода слышны вместе; generated context не выдан за реальный монтаж |
| ASR/localization не даёт однозначных boundaries | Нет proportional/linear guess и splice; доступен сохранённый TTS для отдельной локализации без нового render |
| Выпуск начинается/заканчивается target; сосед меняет speaker | Missing context или неподдержанные bindings явные до оплаты; чужой speaker не прочитан target voice |
| Чтение старого frozen `/1` после обновления | Прежние hashes/body/approval не меняются; fresh prepare/revise создаёт contextual `/2` |
| Итоговая середина другой длины | Честная delta/timeline, без atempo/time-stretch и замены исходных соседних предложений |
| Повторяющееся «ыыыааа» при наличии VAD speech | Нет обещанного semantic detector pass; для отдельного детектора нужны реальные размеченные fixtures |

**Исторический target-only `/1` запуск.** Два дефекта Snow уже подтверждены пользователем. 13 ноября 2026 продуктовый CLI подготовил ниже два proposals с `context_seconds=3`, без API/TTS. На этапе этого prepare у обоих были `approved=false`, `requests_submitted=0`: предложения ожидали exact approval, стыков новой записи ещё не было. Original master SHA-256 `fd37ecb9fb4eeeeb810ceefa3855ce5cf30117055f09704a5c402e11e7b4f4c2`, PCM24 mono48k, 737.84 s. Диапазоны извлечены из сохранённых источников, не из новой ASR-проверки.

| Human timestamp / target | Replacement text, дословный source sentence | Предложенный replace range | Preview с соседями |
|---|---|---|---|
| `03:40`, «обычной» | В обычной округе колонки он насчитал семьдесят три смерти. | `[10549200,10715776)` frames; `219.775000–223.245333 s` | `[10405200,10859776)` frames; `216.775000–226.245333 s` |
| `07:32`, «услышал» | Когда он услышал, что всё валят на колонку, то сказал знакомому врачу, что тщательное расследование эту версию опровергнет. | `[21662656,22012066)` frames; `451.305333–458.584708 s` | `[21518656,22156066)` frames; `448.305333–461.584708 s` |

Полные proposals/preview: `tmp/audio-local-replacement-2026-11-13/{ordinary-district,heard-verb}/export/`. Proposal hashes: `ordinary-district` — `362d95da42d98979f66c1e4e4bfb733ebb5d0e086c551e857901a8597290ef11`; `heard-verb` — `b6ed9b2d230e060e526c206947b6646e5ae8fc190ef90ac0c174ddcaefde290a`. Для диапазонов authoritative целые frames; секунды округлены только для показа.

Источник — сохранённый master assembly result `ee9f1b35-4487-548d-a798-4fe3d8c09f65` и ScriptRevision runtime ref `532cc562-3ab2-5eaa-8d07-e392a39fe951`, hash `0f30c6758d1c4ad197cea57a69d59c6d4998a0780fc2a69f0df3480e3fa3d7c7`. Эти данные не зависят от снятого экспериментального сервиса. Пользователь подтвердил оба дефекта; автоматический review/uncertain не меняет человеческий статус. После новой генерации нужно заново прослушать сами исправленные слова и **оба** стыка каждого предложения. Предсказать диапазоны правого стыка в новой записи до измерения replacement нельзя: они вычисляются по его фактическому числу frames.

**Фактический запуск после exact approval, 13 ноября 2026.** Выполнены два коротких TTS, по одному на proposal, и joint apply. Создан master ref `6b3791d9-d1de-5412-8293-3ccecc404eab`, version `628498ec-3363-5b16-b678-d14079234cd4`: 35 533 934 frames / 740.290291667 s, увеличение на 117 614 frames / 2.450291667 s. Новые spans: `219.775–225.055 s` и `453.115–461.035 s`. Root mechanical audit в `tmp/audio-local-replacement-2026-11-13/verification.json` подтверждает сохранность original, побитное равенство PCM вне замен, непрерывность timeline до endpoint и хеши четырёх join previews; результат — `master/result.json` того же каталога. Usage суммарно 50 input / 330 audio tokens; estimate по frozen free тарифу — $0, invoice не проверен. **Акустическое прослушивание исправленных слов и всех четырёх стыков pending**: `listening_status=not_reviewed`, `production_ready=false`; правильность звучания этим аудитом не установлена.

**Отдельная contextual-проба heard-verb.** `tmp/audio-local-replacement-2026-11-13/context/comparison.json`: TTS озвучил выбранное предложение и по одному соседнему, generated context — 15.2 s; после сохранённой ASR-localization middle `[282476,647752)` при 48 kHz — 365276 frames / 7.609916667 s. В comparison-B окружение осталось исходным PCM, tempo adjustment не применялся; полного нового master эта проба не создала. Пользователь предпочёл B: «второй вариант мне больше зашел». Это обратная связь о сравнении, а не записанный CLI audition verdict. ordinary-district остановлен HTTP 429 Free Tier. `tempo/` и comparison-A — отброшенные пробы, не продуктовая коррекция темпа.

Повторная offline-проверка сохранённого heard-verb новой цепочкой extraction24k → render48k → splice дала те же 365276 frames / 7.609916667 s и delta 15866 frames. `context/production-validation/splice.json` подтверждает неизменность prefix/suffix и frozen файлов, отсутствие изменения темпа; новых provider requests не было. Новый цельный `context/production-validation/contextual-preview.wav` отдельно ещё не прослушан. Механическая проверка и предпочтение экспериментального B не объявляют acoustic acceptance готовой фичи.
