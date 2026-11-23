# Blind listening protocol for Russian documentary speech

## Record the samples

The [comparison plan](comparison-plan.json) pairs five models, two candidate Russian voices per model and two frozen excerpts for 20 cells. The voices are alternatives for a single narrator. Record the exact model, voice, native settings, quota and attempts for every cell. A missing quota or unsupported configuration leaves the cell incomplete.

The baseline uses `direction={"style":""}` and the same profile for both excerpts. Send the original turns together in the supported representation, with their boundaries intact. Preserve native defaults. Use the first technically playable, complete take for each cell. Log transport retries separately from artistic retakes, including errors, bytes, usage and actual cost. An audible performance mistake belongs in the rating.

## Prepare a blind listening copy

Preserve each lossless original. Decode it fully and check duration, loudness, true peak, leading and trailing silence, cuts and clipping. Verify the opening, final line and turn order against the cue sheet, the selected model against the profile, and the source selectors against both accepted reviews.

Use a common lossless playback format and measured loudness. Record target LUFS and true peak, measured values and applied gain. If the target requires dynamics processing, resolve the policy for the entire panel. Retain the original pace, timing, mix and dynamics. Record a duration outside the 45–75 second window as observed.

Assign random blind IDs. Keep the mapping to the profile, request, take, original and playback hashes outside the listening package. Remove provider names, prices, model identifiers and previous judgments from visible metadata. Note any recognizable voice. Give each of three to five independent Russian speakers a separate seeded order. Balance excerpts, separate samples from the same profile, use blocks of five and calibrate listening volume using a separate signal. Listeners use a quiet space and the same headphones across their session.

## Three listening passes

1. **Hear the audio alone.** Record naturalness, intelligibility, emphasis, pauses, continuity, voice consistency and willingness to continue. Give timestamps and describe what you heard. If you stopped early, record the last point heard. Lock the first scores before showing the text.
2. **Check the words.** Compare the exact reference with the recording. Pay particular attention to names, quantities, negations, qualifiers, altered words and spoken instructions. A recognizer, if used, receives only the audio on its first pass. Resolve discrepancies by listening and mark uncertain pronunciations accordingly. Record the actual coverage when ASR is unavailable.
3. **Read the context.** For disputed joins, pauses, endings or emphases, listen again with the complete saved script. Record this judgment separately. Refer any change of the source wording to the script owner.

The [rating form](rating.schema.json) uses 1 for a serious impediment, 2 for a distracting defect, 3 for tolerable but persistently artificial delivery, 4 for comfortable, natural speech and 5 for fluent, expressive delivery that invites continued listening. Use null when the listener could not judge.

## Shortlist and direction test

Compare both excerpts for each exact profile. Include individual scores, disagreement, the weakest excerpt and timestamped fidelity or signal defects. Discuss differences and reveal identities after collecting the independent ratings. Report missing cells. A tie or an incomplete shortlist is a valid outcome.

For the two qualified finalists, freeze a third ending excerpt from an accepted script. Compare baseline delivery with the same short English direction in each provider's supported direction field. Keep spoken text, Russian output language, voice, codec, loudness and all other settings fixed. A provider requiring inline tags needs an explicit plan and a check that the tags are silent. Two profiles × three excerpts × two modes × two takes gives 24 cells. Compare every take. Keep direction only if listeners hear an improvement and the words remain intact.

The later qualification covers the full Apollo and rain episodes, a two-host GPS scene and a local replacement with neighbouring sentences. Short sample results cover only the excerpts heard.
