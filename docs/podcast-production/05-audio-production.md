# Audio production and listening

The audio layer takes a specific accepted script and turns it into performance instructions, synthesis requests and an assembled master. It also needs a way to determine what was actually said. A technically valid WAV can contain the wrong date, a dropped word or an unnatural pause at an edit.

## Performance and rendering

Bind each voice to a provider, model, language, format, sample rate, options, rights and qualification. Keep spoken text separate from provider controls. A pronunciation hint or normalization of a number that changes its meaning requires a script correction and renewed review.

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

Choose boundaries that retain an utterance's rhythm and a voice's continuity. A provider context feature, a direction and surrounding words submitted as speech have different effects. A request manifest identifies which spoken characters it covers and which context, if any, should be discarded during editing. Preserve provider-specific measurements and native responses.

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

A reusable rendering depends on the actual speech and pinned synthesis settings. Two editions can reference the same immutable bytes with different script bindings. Approval of the first is not automatically approval of the second. Reserve the maximum eligible cost before submission. Record an uncertain send and reconcile it before retrying. Local processing of an already received response can be repeated as a new operation.

## Assembly, transcript and time

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

A master has a single PCM frame origin. Store sample rates and exact frame boundaries for each input, the resampling relationship and the new master. The accepted episode must contain every spoken character in order with no gaps or duplicates. A complete render applies loudness adjustment at the master stage. Do not conceal a failed synthesis by inserting silence or changing playback speed.

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

Use independent speech recognition and human listening to evaluate what was said. Forced alignment can locate known words once the recognized text is established; it cannot independently confirm that a missing word was audible. Separate suspected differences in numbers, units, names and negation from harmless orthographic variants. A low-confidence difference is a candidate to inspect, not an instruction to regenerate.

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

## Contextual local replacement

For a reported defect, bind the affected one or two sentences and their exact interval to the existing master. Show the proposed interval, original wording and replacement wording. Approval belongs to that proposal. A changed interval or text requires a new decision before synthesis.

Render the approved target with its neighboring sentences and the same voice and settings. Locate the target's beginning and end in that new recording, extract the corresponding samples and splice those samples into the immutable parent. This retains the provider's natural lead-in and cadence. Preserve the adjacent original audio and measure the new duration rather than stretching the replacement to fit.

Provide one continuous after-preview containing both joins and the complete corrected passage. Save the new master, timeline delta, frame map and unresolved timing references. Keep playback, captions and chapters pinned to the same edition. A wrong parent hash, an uncertain boundary or missing context prevents application to that master.

## Operator entry points

The [episode CLI](episode-production.md) owns production rendering. The following dedicated entry points cover evaluation and repair. Each supports `--help` without calling a provider.

| Purpose | Entry point |
| --- | --- |
| Prepare, compare and reprocess voice samples | [audio_evaluation_cli.py](../../backend/src/podgen/audio_evaluation_cli.py) |
| Inspect possible discrepancies in spoken words | [audio_fidelity_cli.py](../../backend/src/podgen/audio_fidelity_cli.py) |
| Assemble and export a complete listening master | [audio_assembly_cli.py](../../backend/src/podgen/audio_assembly_cli.py) |
| Review, render and apply a contextual replacement | [audio_local_replacement_cli.py](../../backend/src/podgen/audio_local_replacement_cli.py) |

The [voice comparison protocol](research/2026-11-12-speech-synthesis.md) describes blind listening and provider selection. The [machine-readable audio contracts](audio/) define saved comparisons and assemblies. Distinguish a local render failure, a provider refusal, an editorial change and a performance retake in the record. A completed assembly still needs acoustic review.


The following commands use local configuration files and an existing project and actor. Preparation constructs the comparison or fidelity request. The listening command exports a prepared batch for audition. The Fish header repair flag belongs only to profiles with the diagnosed header issue.

```sh
python -m podgen.audio_evaluation_cli --project PROJECT --actor ACTOR \
  --objects OBJECTS --env-file .env prepare --config comparison.json --export-dir comparison
```

```sh
python -m podgen.audio_evaluation_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  listen --config comparison.json --export-dir listening/UUID --batch-id UUID \
  --repair-observed-fish-header
```

```sh
python -m podgen.audio_fidelity_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  --env-file .env prepare --config fidelity.json --export-dir fidelity-export
```

```sh
python -m podgen.audio_assembly_cli --project PROJECT --actor ACTOR --objects OBJECTS \
  build --config assembly.json --export-dir assembly-export
```
