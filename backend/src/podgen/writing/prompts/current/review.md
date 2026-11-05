# Review the spoken episode

Read every supplied turn in order. Reconstruct the question, answer and reasoning from the spoken words. Compare these with `story_intent` and identify any step the listener would need to supply. Report `reading_mode=informed` because support and story intent are supplied.

Check claims against the attributed evidence: counted populations and periods, units and operations, dated actions, source dependence and conditions for causal conclusions. Keep material objections and uncertainty with the conclusion they affect. Check the literal wording, including negation and place value. Identify useful callbacks, repeated explanations and the basis of the duration estimate.

For each material problem, locate the passage, explain its effect on understanding and request a specific change at the right scale. Identify passages worth preserving. Anchor to the supplied zero-based utterance index and an exact nonempty substring of that turn's `text`, preserving case and punctuation. Connect findings to multiple passages where needed; locate an omission at its real neighbors or the promise it affects.

Return the reconstructed listener answer, audible limits, strengths, findings, missing-evidence questions and duration note. Lists may be empty. Recommend `keep` when the text is ready and no material needs remain; `revise` for required repairs supported by available material; `needs_material` when a specific evidence gap prevents an adequate revision.
