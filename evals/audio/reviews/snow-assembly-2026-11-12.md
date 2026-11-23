# Snow master assembly review

At commit `4d56056`, the reviewer checked the source selections, PCM, output hashes and ASR receipt for a 34-utterance recording. The two parts covered utterances 0–17 and 18–33. Speakers, text and IDs matched the [accepted script](../fixtures/snow-accepted-v1/source.json). Script ID `532cc562-3ab2-5eaa-8d07-e392a39fe951` had SHA-256 `0f30c6758d1c4ad197cea57a69d59c6d4998a0780fc2a69f0df3480e3fa3d7c7`.

| PCM | Frames | Rate | Range in the final master |
| --- | ---: | ---: | --- |
| Part 1 | 8,933,760 | 24 kHz | [0, 17,867,520) |
| Part 2 | 8,774,400 | 24 kHz | [17,867,520, 35,416,320) |
| Assembly before gain | 17,708,160 | 24 kHz | Both complete parts |
| Final master | 35,416,320 | 48 kHz | [0, 35,416,320) |

The pre-gain PCM matched the concatenation of both decoded parts byte for byte. The mono, 24-bit master runs 737.84 seconds, with global gain of −0.85 dB, measured −22.21 LUFS and −2.12 dBTP. The master and playback share SHA-256 `fd37ecb9fb4eeeeb810ceefa3855ce5cf30117055f09704a5c402e11e7b4f4c2`. The join occurs at 372.24 seconds. The review excerpt covers master frames [17,387,520, 18,347,520), or 362.24–382.24 seconds, and its PCM and hash matched.

One independent ASR submission covered the entire playback. The submitted FLAC decoded to the same PCM and the recognizer received no expected text. The review found 67 script differences, one transcript/segment conflict and 59 zero-length timing diagnostics. All 68 candidate clips were within the master and extracted successfully. The recognized second part began around 372.53 seconds and the final segment ended at 737.52. The reviewer verified assembly and clip provenance without listening. Spoken-word fidelity and the audible quality of the join were still open.

The [audio specification](../../../docs/podcast-production/05-audio-production.md) describes the subsequent human-confirmed local replacement workflow.
