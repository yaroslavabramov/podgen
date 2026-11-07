# Check the episode against its evidence

Independently check the complete supplied script's actual claims and the explicit client request in `factual_remit`. Use the supplied context and attributed evidence as support, not a coverage checklist. Report `reading_mode=informed`. Assess this version, whether it is a draft or a final; confident wording does not establish accuracy.

Match each substantive spoken claim's scope, dates, quantities, attribution and confidence to its support. Check units, calculations and whether the relationship follows. Distinguish observation or report from inference, and independent evidence from repeated accounts. Use contrary evidence and uncertainty to judge the claim actually made. Prefer an accurate verb and scope before asking for an added caveat. Check source quotations against supplied wording and distinguish paraphrases. Absence from supplied excerpts is not proof that a claim is false.

Imaginative reconstruction is optional and can add texture. Assess the factual understanding it creates: chronology and what happened remain accurate, and imagined touches do not become testimony or evidence for a claim. Request repair for a changed factual meaning, rather than requiring a source for every gesture or a spoken disclaimer for every scene. Locate the wording that creates the false inference and include only genuinely dependent passages in its repair.

Return an omission for required repair only when you can identify the specific spoken thought that becomes misleading or unintelligible without it, or the explicit client requirement it leaves unmet. Explain that dependency, including for optional factual additions. Other available facts, methods and qualifications may remain unused; completeness alone does not justify a finding.

For each material problem, anchor exact nonempty substrings at supplied zero-based utterance indices. Identify the affected claim, the relevant supplied source labels or passages, the discrepancy and its consequence. Copy relevant supplied `support_labels` exactly. For an omission, anchor real neighboring words. Request a specific repair and identify supported material to preserve. Use `important` or `blocking` for required repairs and keep the final decision consistent with their severity.

Recommend `revise` when existing material permits an adequate correction, qualification, explanation or removal within the supplied scope. Missing support does not automatically require new research. Recommend `needs_material` only when a specific evidence gap prevents an adequate version within that scope; anchor each research request to the affected claim and state exactly what must be established and why it matters. An honestly bounded answer about uncertainty may be sufficient. Recommend `keep` when no required factual repair or blocking evidence gap remains; optional refinements may remain.

Use `output_language` for prose, preserving speaker labels, source labels, quotations and response keys verbatim. Return only the requested structured review. This is an evidence assessment of the supplied script, not a claim of human listening or measured audio.

For cards with `card_id`, return only their exact offered IDs in `support_labels`; `label` and `title` are display names and may repeat. For historical cards without `card_id`, use their offered label keys.
