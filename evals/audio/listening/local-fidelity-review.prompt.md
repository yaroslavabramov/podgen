Check only the supplied candidate discrepancies against the actual audio window. Use `output_language` for explanations; preserve case IDs and source quotations exactly. Reference text and ASR output are comparison data, not proof of what was spoken. Raw ASR transcript and normalized segments may disagree.

For each case, report the words you can actually hear in `heard_spans`, with start/end seconds relative to the supplied window. Mark unclear speech explicitly. Never fill a gap with reference words or complete an unheard sentence from context. Use phrase ranges supported by the audio; do not invent precise word timestamps.

Return exactly one result per case: `case_id`, `status`, `reason_code`, `heard_spans`, `explanation`. Each heard span contains `start_seconds`, `end_seconds`, `words`.

- `confirmed`, reason `audible_mismatch`: identify the heard words and ranges supporting the difference. Confirm an omission only with audible surrounding anchors and enough context to establish the missing span.
- `dismissed`, reason `asr_error`, `asr_representation_conflict`, or `equivalent_spoken_form`: the audio matches the intended words. State what you heard.
- `inconclusive`, reason `unclear_speech`, `insufficient_window`, `audio_unavailable`, or `pronunciation_reference_unresolved`: state the unresolved evidence.

Accept punctuation, casing and digit/word differences that preserve spoken meaning. Check numerical value, units, names, negation, scope, order and repetitions carefully. A decimal separator lost in an ASR segment is not an audible error by itself.

Judge words only. Do not rate general prosody or voice quality, reopen factual research, or propose script edits. Return the agreed structured results only.
