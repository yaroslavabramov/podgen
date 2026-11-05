# Select fields for unresolved review targets

For every `target_id` in `invalid_targets`, locate the candidate field that fits the existing `editorial_judgment`. Copy an exact path from `allowed_review_targets` into `selected_path`. Paths address the proposal itself. Choose null if none fits; do not select an unrelated parent just to obtain a valid address.

Return exactly one choice per offered target, preserving labels and paths verbatim. This task only repairs locations. Keep decisions, reasons, priorities, requested changes, material references and research questions unchanged. Do not revise or approve the proposal.
