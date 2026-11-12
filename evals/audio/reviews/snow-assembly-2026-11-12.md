# Независимое чтение фактической сборки Snow

Ревью `4d56056` и локальных артефактов. Код не менялся, тесты и API не запускались. Аудио не прослушивалось; проверка bytes/frames не подтверждает произнесённые слова.

## Source и покрытие

- Native script: `532cc562-3ab2-5eaa-8d07-e392a39fe951`, hash `0f30c6758d1c4ad197cea57a69d59c6d4998a0780fc2a69f0df3480e3fa3d7c7`.
- Две сохранённые source-части покрывают indices 0–17 и 18–33. Все 34 speaker/text и utterance IDs совпадают с fixture; fixture script дословно совпадает с native outcome `evals/reports/2026-11-12-review-audit-cycle/review-outcome.json`.
- Assembly manifest связывает их с result refs `043608ea-f873-5d75-941f-876f425d660a` → `269eb68a-7835-5211-a79d-11db5818a174`. Это правильный порядок; нет потерь, повторных source selections или изменения текста.

## Фактические PCM и frame map

Источник проверки: `tmp/audio-full-snow-2026-11-12/assembly/export/`.

| Артефакт | Frames | Частота | Диапазон в final master |
|---|---:|---:|---|
| Part 1 decoded | 8 933 760 | 24 kHz | [0, 17 867 520) при 48 kHz |
| Part 2 decoded | 8 774 400 | 24 kHz | [17 867 520, 35 416 320) при 48 kHz |
| Assembly до gain | 17 708 160 | 24 kHz | Полные две части |
| Final master | 35 416 320 | 48 kHz | [0, 35 416 320) |

Хеши исходных WAV и PCM совпали с frame-map. PCM файла `private/assembly-pcm-before-gain.wav` побайтно соответствует concatenation полных decoded parts в указанном порядке (сопоставлены SHA-256 PCM). Его WAV SHA-256 совпал с result blob.

Фактический final master — mono/24-bit, 737.84 секунды. SHA-256 `fd37ecb9fb4eeeeb810ceefa3855ce5cf30117055f09704a5c402e11e7b4f4c2` совпал с `master_pcm_blob` и `playback_audio_blob`; это один файл. Frame map непрерывна, заканчивается точно последним measured frame. Global gain −0.85 dB, измерено −22.21 LUFS / −2.12 dBTP; per-part gain не применялся.

Стык находится в master frame 17 867 520 = 372.24 секунды. `join-1.wav` — точный PCM диапазона master [17 387 520, 18 347 520), то есть 362.24–382.24 секунды, 960 000 frames. Проверены совпадение PCM с master, PCM hash и WAV hash с result. Это проверяет происхождение и границы клипа, а не естественность перехода.

## ASR именно итогового playback

`tmp/audio-full-snow-2026-11-12/fidelity/export/`: одна отправка, `candidates_generated`. Recognition receipt ref `9435d9e4-8f0f-58b7-ae55-afe12743e987`; submitted audio hash совпадает с final master, range [0, 35 416 320), duration 737.84. Manifest содержит `expected_text_sent_to_recognizer=false`; транспортный FLAC имеет сохранённую proof побайтного PCM equality.

Получено 68 candidates: 67 script discrepancies и 1 transcript/segment conflict. Отдельно сохранены 59 zero-length timing diagnostics. Отчёт остаётся `audio_review_status=pending`, `production_ready=false`.

Распознанный переход содержит окончание «А дело тем временем продолжается» (до 371.81) и начало «Еще один эпизод…» (с 372.53). Это corroboration последовательности от независимого ASR, не акустическое подтверждение точности всех слов или качества стыка. Числовые формы, имена, окончания и отрицания среди candidates требуют реального аудио; список ASR diff не разрешает сам по себе repair.

Дополнительно прочитаны начало и конец ASR. Первые segments начинаются с «Лондон» на 0.10; последний — «живых» на 737.12–737.52. В начальной реплике сохранены deletion candidates для «с» и «а»; в последней — «Сноу» → «снова он», «не» → «ни», «и не» → «ни». Эти расхождения не признаны ошибками TTS. Все 68 candidate ranges находятся внутри master, все соответствующие клипы имеют статус `extracted`.

## Итог и незавершённое

В проверенной сборке, source coverage и фактической frame map actionable находок нет. Метаданные правильно уточняют `basis=source_selections_and_complete_pcm_parts`, `spoken_word_fidelity=pending`, `acoustic_verdict=pending`.

Позднее прежний whole-fragment repair снят с использования. Действующий контракт локальной замены по подтверждению человека описан в [спецификации аудио](../../../docs/podcast-production/05-audio-production.md). Это ревью сборки не подтверждает диапазоны новых замен.
