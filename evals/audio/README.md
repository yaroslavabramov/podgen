# Speech evaluation

The [Russian documentary fixture](fixtures/ru-documentary-v1/manifest.json) contains two continuous excerpts from complete, accepted scripts. Use the `fragments` array in [fragments.json](fixtures/ru-documentary-v1/fragments.json) with the [comparison schema](../../docs/podcast-production/audio/comparison.schema.json). This provides the texts and empty style direction. Profiles, credentials and spending limits belong in the operator's runtime configuration.

| Clip | Source run | Utterances (zero-based) | Tokens | Characters | Estimated duration |
| --- | --- | --- | ---: | ---: | --- |
| `apollo-narrative` | `78a70249-0eb2-400d-beab-0ba47c23cdba` | 0–1 complete | 125 | 815 | 50–68 s |
| `rain-experiment` | `e89d67be-f630-4e3d-bb05-f3bf637c797a` | 3 from character 32, then 4–6 complete | 119 | 850 | 48–65 s |

The token counts split on whitespace and include standalone punctuation. The estimates assume 110–150 such tokens per minute. Unicode and UTF-16 lengths agree for these texts. Measure the actual recording against the 45–75 second listening window and record any difference.

The [episode records](fixtures/ru-documentary-v1/episodes/) contain full scripts, original runtime references, `artifact-jcs-v1` hashes and both reviewers' final decisions for each script. Utterance identifiers use `uuid5(script_ref.record_id, "utterance:<index>")`. The [clip files](fixtures/ru-documentary-v1/clips/) record the exact source spans, listening references and critical words. Two newline characters separate utterances in each listening text. Adapters record the actual join or per-turn representation and provider request hash separately.

The [comparison plan](listening/comparison-plan.json) defines 20 voice/model/excerpt cells. In the original comparison, ElevenLabs, Gemini, Inworld and Fish were configured on free plans, while MiniMax was unavailable. That allowed at most 16 cells before quota checks. Use the [blind listening protocol](listening/protocol.md) and [rating schema](listening/rating.schema.json) to record available samples and missing cells. The manifest lists deferred checks for additional names, decimals, endings and full episodes.

The [Snow script](fixtures/snow-accepted-v1/source.txt), [source record](fixtures/snow-accepted-v1/source.json) and [two human-confirmed repair cases](repair/snow-human-cases.json) support local replacement evaluation. The [audio specification](../../docs/podcast-production/05-audio-production.md) describes the operative repair procedure.
