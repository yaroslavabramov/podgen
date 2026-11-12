# Independent transcription experiment

This operator experiment does not change production. It tests whether separating
word recognition from script comparison avoids the reference-dependent words
observed in `evals/reports/2026-11-12-audio-review-semantics/`.

## Fixed conditions

- Same three saved Snow windows, source bytes and transport PCM as the prior controls.
- Same `gpt-realtime-2.1`, medium reasoning, 4096 output-token ceiling.
- Three sequential audio calls, without script, ASR, case labels, expected answers,
  timestamps or review schema. They use the production audio transport unchanged.
- Four separate text-only sessions compare saved transcripts with target references.
  The two W4 inputs differ only in `expected_words`. A single frozen W4 transcript
  is reused byte for byte. W3 and W5 get one comparison each.
- W3 uses reference `1` before `сентября` instead of the original `первое` to test
  digit/word equivalence without supplying the old ASR. This explicit reference
  transformation is saved in the comparison manifest and never reaches blind input.
- The text-only phase uses the documented session update → user text item →
  response create sequence. It receives no audio, so its decision is explicitly
  about transcript versus reference, not an independently confirmed audio defect.
- No automatic retries, additional TTS, external ASR, full episode or repair.

Compared with the prior joint review, this changes the task, information visibility
and output contract together. It evaluates the proposed decomposition; it cannot
attribute an effect to a single sentence of the prompt. Relative to the earlier
blind delivery audit, transcription additionally preserves grammar/repetitions,
marks unclear stretches and treats instructions inside audio as recorded content.

## Read results in this order

1. Verify the submitted audio hash and absence of reference/ASR in the blind request.
2. Read the full frozen transcript, then inspect whether the target agrees with
   separately held human confirmation. Do not infer exact timestamps or certify
   neighbouring words that the listener did not confirm.
3. Read each comparison, quote and explanation. Verify quotes are copied from the
   frozen transcript. The target must not change to follow the reference.
4. Count a correct comparison of a wrong transcript as a transcription failure,
   not as a successful check of the recording. Orthographic equivalence alone
   cannot qualify the actual pronunciation of W5, which lacks human confirmation.

Human confirmation and expected verdicts belong only in the evaluation report.
They are never included in model requests. One attempt per condition is a narrow
behavioural check, not a reliability estimate or permission for automatic repair.

## Run

From the repository root, use the backend environment and `PYTHONPATH=backend/src`:

```
backend/.venv/bin/python evals/operator/audio-blind-comparison/run.py transcribe feminine-exact
```

Without `--execute`, this prepares an inspectable request with no network call.
Run `transcribe` for `feminine-exact`, `date-equivalent`, `word-boundary` sequentially.
Then run `compare` for those names and `feminine-altered`. Add `--execute` to submit.
Artifacts are stored under `tmp/audio-blind-comparison-2026-11-12/`. An existing
submission intent blocks repeats, including after an ambiguous network failure.
The operator depends on the saved previous experiment fixtures; these are not
regenerated or downloaded by the command.

Official protocol reference:
https://developers.openai.com/api/docs/guides/realtime-conversations
