# Select exact plan review addresses

This is an address-selection repair, not a new editorial review or a plan revision. Read the supplied candidate and original judgments. Preserve the decision, reasons, priorities, requested changes, material references, research requests and already valid targets. Do not add evidence or approve the plan yourself.

`invalid_targets` lists the rejected addresses. For every offered `target_id`, select the field that actually fits its `editorial_judgment`. Copy an exact path from `allowed_review_targets`. Paths are relative to the PlanningOutput inside `candidate_plan`: an act field can be `/plan/acts/0/intended_change_in_understanding`, while episode-wide limitations are at `/limitations`. The surrounding `/candidate_plan` prefix is not part of these addresses. Do not guess a new path or merely strip a prefix from a nonexistent field.

If no offered field fits the original judgment, choose `selected_path=null`. This leaves the review unresolved and stops acceptance; it is better than an unrelated address. Do not use a broad parent just to make an invalid address pass.

Return only PlanTargetChoices: `choices` with exactly one `{target_id, selected_path}` for every supplied invalid target, including null selections. Copy IDs and paths verbatim, without translation. Do not omit, duplicate or invent target IDs. The server applies these selections to the saved review and validates it again; this call does not grant factual or production approval.
