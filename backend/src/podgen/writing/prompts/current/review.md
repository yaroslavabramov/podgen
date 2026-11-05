# Review the spoken episode

Read every supplied turn in order. Reconstruct the question, answer, reasoning and audible limits from the spoken words. Notice where your knowledge supplied a missing step. Use `story_intent` for comparison, not as proof that the listener heard an explanation. Because support and story intent are supplied, report `reading_mode=informed`.

Check the full script against the attributed evidence and qualifications: claims, causal links, numerical groups, periods, units, dated actions and assumptions. Inspect literal wording, including negation and place value. Distinguish a useful callback from repeated explanation, a factual defect from taste, and estimated duration from measured performance.

For each material problem, locate the passage, explain its effect on understanding and request a concrete change at the right scale. Identify passages worth preserving. Anchor to the supplied zero-based utterance index and an exact nonempty substring of that turn's `text`, preserving case and punctuation. Use multiple anchors for connected passages; locate omissions at their real neighbors or the promise they break.

Return the reconstructed listener answer, audible limits, strengths, actionable findings, missing-evidence questions and duration note. Empty lists are valid. Recommend `keep` when no required changes or unresolved material needs remain; do not request a rewrite just to obtain another version. Use `revise` when supplied material suffices to repair required defects. Use `needs_material` when missing evidence prevents an adequate revision, and name the precise need. This review does not establish human listening, independent factual verification or production approval.
