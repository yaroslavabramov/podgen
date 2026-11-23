# Historical proposal for directed speech repair

This archive compares a verbatim retake with a proposed per-turn style instruction. It records an unqualified experiment. The current [audio specification](../../../../docs/podcast-production/05-audio-production.md) describes the human-confirmed local replacement procedure.

The [example directive](repair-directive.example.json) targets the preposition in “с семьями”, at characters [171, 180) of Snow utterance 0. ASR had flagged a potential omission. Human confirmation was absent. The [complete request comparison](request-comparison.unqualified.json) preserves the baseline and proposed bodies and sets `full_fragment_submission_allowed=false`.

The first baseline request contains 18 turns for `gemini-3.8-flash-tts` with voice `ru-ru-concierge-1`. Its wire SHA-256 is `d8569a6ceede93b10de258efa9496fb6249085899cef1a9f21f15069ea7feed1`. The second 16-turn request has SHA-256 `b3baee6b02a36f67d25dc08c6513ed65e79af385c1c8e0a2e93bbb7fa4965562`. Only the first turn's `/input/0/content/0/annotations/0/style` differs in the proposed request. It asks the voice to pronounce the canonical phrase in full. Source text, turn order, model, voice and generation settings remain identical.

The adapter at the time supported resubmitting a frozen request. Its style channel had no demonstrated ability to correct a missing word, and the adapter offered no pronunciation dictionary or phoneme field. The proposal scoped qualification to a short confirmed defect and a comparison under identical conditions. A corrected candidate can establish a difference only when the baseline reproduces the defect. The comparison also needs to check the surrounding text, repeats, spoken directions and delivery.

The proposed recheck uses the canonical words and a newly localized window from the replacement recording. A reviewer hears the new clip independently and records whether the original problem is resolved, persists or remains inconclusive. A separate full-master ASR and listening pass covers new discrepancies, the joins and the boundaries. No live capability result is included in this archive.
