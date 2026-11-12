Review only the supplied cases for spoken-word fidelity. Compare the actual audio with the intended script in `expected_words` and `expected_context`. ASR is a fallible recognition of the recording, supplied only to help locate a possible discrepancy. An ASR error is not a recording defect. Use `output_language` for explanations; preserve case IDs and source quotations exactly.

Report the words you actually hear in `heard_spans`, with start/end seconds relative to the supplied window. Use phrase ranges supported by the audio; ASR words and timestamps do not establish what was heard. Mark unclear speech explicitly rather than completing it from either the script or ASR.

Return exactly one result per case: `case_id`, `status`, `reason_code`, `heard_spans`, `explanation`. Each heard span contains `start_seconds`, `end_seconds`, `words`.

- `confirmed`, reason `audible_mismatch`: the audio differs from the intended script. Identify the heard words and the intended words they differ from. Confirm an omission only with audible surrounding anchors and enough context to establish the missing span. Disagreement between audio and ASR alone never warrants this status.
- `dismissed`, reason `asr_error`, `asr_representation_conflict`, or `equivalent_spoken_form`: the audio matches the intended script, regardless of what ASR says. State the heard words and why they match.
- `inconclusive`, reason `unclear_speech`, `insufficient_window`, `audio_unavailable`, or `pronunciation_reference_unresolved`: state the unresolved evidence.

Spelling, word spacing, punctuation, casing, and digit/word notation alone do not establish a difference in sound. Dismiss equivalent spoken forms. Check numerical value, units, names, negation, scope, order and repetitions carefully. A decimal separator lost in an ASR segment is not an audible error by itself.

Judge words only. Do not rate general prosody or voice quality, reopen factual research, or propose script edits. Return the agreed structured results only.
