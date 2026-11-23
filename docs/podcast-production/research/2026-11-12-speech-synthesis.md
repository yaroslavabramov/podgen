# Qualifying documentary speech synthesis

This research note defines the listening protocol for the Russian documentary fixtures. It describes the candidates and proposed sample sizes on 12 November 2026. A protocol alone does not report a winner. The fixture manifest pins the scripts and exact excerpts by checksum. [Evaluation fixtures](../../../evals/audio/fixtures/ru-documentary-v1/manifest.json) · [Source episodes](../../../evals/reports/2026-11-12-two-topic-cycles/README.md)

## What the listener is judging

A voice can sound convincing on a single sentence and lose its phrasing over a paragraph. Documentary narration needs meaningful emphasis, changes of pace and enough continuity to follow an explanation. Assess the same voice on connected scenes and complete episodes, as well as on short samples. Record where a listener hears a word, emphasis or transition differently from the intended reading. A listener's strong preference in one language or genre only qualifies that particular use. [Long-form TTS evaluation](https://www.isca-archive.org/ssw_2019/clark19_ssw.html) · [Multi-sentence TTS](https://www.isca-archive.org/interspeech_2022/makarov22_interspeech.html)

## Candidate profiles

| Candidate in the November comparison | Capability to qualify with a real Russian sample |
| --- | --- |
| ElevenLabs Eleven v4 | Evaluate natural delivery, pronunciation controls and tagged dialogue in the appropriate endpoint. Verify which response supplies alignment for the chosen model. [Models](https://elevenlabs.io/docs/overview/models) · [Dialogue with timestamps](https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert-with-timestamps) |
| Gemini 3.8 Flash TTS | Compare a natural reading with a short delivery direction. Exercise single and multi-speaker requests and inspect the returned audio format. The Gemini API and Cloud TTS expose different interfaces. [Speech generation](https://ai.google.dev/gemini-api/docs/speech-generation) · [Cloud TTS](https://docs.cloud.google.com/text-to-speech/docs/gemini-tts) |
| Inworld TTS-2 | Check Russian voices, delivery guidance, stability settings and continuity across neighboring utterances. [Steering](https://docs.inworld.ai/tts/capabilities/steering) · [Script synthesis](https://docs.inworld.ai/tts/capabilities/script-synthesis) |
| Fish Drama 3 and S2.1 | Pin the particular model, speaker reference, text/markup handling and output format. Evaluate hosting or voice rights against the selected mode. [Drama 3 API](https://docs.fish.audio/features/text-to-speech/drama-3.md) · [Model catalog](https://docs.fish.audio/developer-guide/models-pricing/models-overview.md) |
| MiniMax Speech 2.8 HD | Check the selected Russian voice, pronunciation and paragraph delivery with the exact synchronous synthesis settings. [Speech API](https://platform.minimax.io/docs/api-reference/speech-t2a-http.md) |

The research also considered Cartesia, OpenAI speech, Qwen3-TTS and Yandex SpeechKit as additional comparators. Availability of a language, style parameter or emotion label is a reason to test a candidate. A usable voice and accurate rendering must still be demonstrated on the material. [Cartesia](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest) · [OpenAI speech](https://developers.openai.com/api/docs/guides/text-to-speech) · [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) · [SpeechKit markup](https://aistudio.yandex.ru/ru/docs/speechkit/tts/markup/tts-markup)

## Direction and segmentation

Compare the chosen voice at its natural setting with the same voice given a short, scene-specific direction. Keep all other parameters and the attempt count equal. Use a stable voice identity across an episode. A scene direction can mark a meaningful contrast, quicken an action or give a result time to register. Keep instructions separate from words to be spoken.

The proposed direction for the controlled comparison was:

```text
Read the supplied script verbatim in the requested output language as an
engaged documentary host speaking to one listener. Let phrasing and emphasis follow the meaning.
Keep the delivery conversational, with enough variation to sustain attention.
```

The control sends no additional direction. Use a provider-specific adapter for supported controls, and verify that intended instructions never become audible speech. Give the director the complete episode or act and its neighboring context. Send the synthesizer a complete thought or exchange within its actual limits. Compare connected rendering with splitting on sentence or character boundaries. For a voice comparison, listen to dry speech at matched perceived loudness.

## Blind comparison protocol

| Stage | Material | Decision |
| --- | --- | --- |
| Capability check | Russian speech, names, direction, adjacent passages and two speakers where available, for each exact profile | Which request features work and which settings reach the service? |
| Voice selection | Five models × two suitable Russian voices × two identical excerpts, 20 recordings of roughly 45–75 seconds | Select two model–voice combinations. |
| Direction | Two finalists × three excerpts × two direction modes × two takes, 24 recordings | Does direction improve delivery reliably? |
| Long form | Render the complete rain and Apollo 13 scripts on both finalists. Include a separate two-host GPS scene. | Assess fatigue, narrative contour, consistent voice, handoffs and joins. |
| Retake | Replace one pronunciation and one poor transition in an assembled recording. | Can the editor repair a local defect while preserving the neighboring performance? |

Use exact contiguous source utterances and frozen script hashes. A narrative passage and an explanatory passage make up the first comparison. Add an ending for the direction trial. Diagnose names, dates, numbers, abbreviations, stress and negation separately. The fixture manifest identifies checks deferred from the first selection.

Preselect voices appropriate to Russian documentary narration. An English demonstration voice poorly represents that decision. Randomize anonymous sample IDs and presentation order. Keep every take, failure, cost and setting. Where a provider has no reproducible seed, the raw response and actual recording identify the take.

Start with three to five independent Russian-speaking listeners to identify clear defects. Elicit paired preferences and exact moments that explain them. This sample supports a diagnostic decision rather than a population estimate. If available, compare a professional dry recording of the same text. A short-sample winner still needs a complete listening pass. [ITU-T P.808](https://www.itu.int/rec/T-REC-P.808-202106-I/en) · [Evaluation of synthetic spontaneous speech](https://www.isca-archive.org/interspeech_2024/edlund24_interspeech.html)

## Audio QA

Transcribe the actual render independently. Align that transcript with the frozen script, retaining differences in names, numbers, negations, omissions and insertions. Review the sound around a discrepancy before deciding which words were spoken. Forced alignment can place a known word in time. It cannot establish that a missing word was actually pronounced. An acoustic reviewer can help locate awkward pauses and joins after calibration against human annotations. [Transcription](https://developers.openai.com/api/docs/guides/speech-to-text) · [Audio understanding](https://ai.google.dev/gemini-api/docs/audio)

Check decoding, sample format, order, duration, clipping, loudness, peaks and both sides of every boundary. Listen in context after each repair. A transcription mismatch is a lead, and a passing technical check leaves the performance decision to the listener. If synthesis actually changes the spoken meaning, refer the affected script version to its editorial owner.

## Cost per accepted minute

Use the rate effective for the selected account, model and date. The original comparison encountered per-character prices, per-UTF-8-byte prices, and separate input-text and output-audio token prices. Cyrillic letters commonly occupy two UTF-8 bytes. Count the string or usage actually billed, including applicable control markup. For each candidate, report accepted audio minutes and total costs for all attempts, failures, retakes and QA. Divide the latter by the former. Keep latency as a separate measurement. [ElevenLabs](https://elevenlabs.io/pricing/api) · [Inworld](https://inworld.ai/pricing) · [Fish](https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits.md) · [MiniMax](https://platform.minimax.io/docs/pricing/overview) · [Gemini](https://ai.google.dev/gemini-api/docs/pricing)

## Handoff to production audio

Bind a rendered candidate to the exact accepted reading outcome and script hash. Preserve voice rights, provider profile, direction, request, response, take identity and measured usage. The audio design separates performance, accepted takes, assembly, transcribed audio and release state. [Audio design](../05-audio-production.md) · [Validated outcome and audio descriptors](../validated-outcome-and-audio-descriptors.md)

A retake can change pronunciation, direction or the context supplied to the synthesizer. If a transition is poor, regenerate a longer connected passage and review its joins. Preserve the good adjacent material and the original PCM. Track retake spending and expose an unresolved defect if the limit is exhausted. Completion for this protocol requires complete rain and Apollo listening passes, a reliable local repair and a record of the evidence behind the chosen voice.
