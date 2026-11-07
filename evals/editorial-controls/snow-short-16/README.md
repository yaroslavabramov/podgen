# Editorial control: Snow, original 16-card assignment

`plan.json` is an editor-authored complete `PlanningOutput` for the existing short assignment. It is a control input for the writer, not a generated planner result, a revised brief, a spoken script or an acceptance record. `status: ready` describes the proposal's research state, not independent editorial approval.

Source packet: `evals/editorial-inputs/current/snow-pump/shared.json`, `editorial-input/2`, 16 cards. Exact file SHA-256: `5a7ac5d3471b1220c3fe1affd0f1eaf807bdf3b6095a655010b25ec009eb0987`. The packet, assignment, full source excerpts and card identifiers are unchanged.

Original request: «Хочу подкаст про Джона Сноу и холеру в Лондоне. Расскажи эту историю как детектив.» Audience: an adult hearing the story for the first time. Solo Russian narration; target 900 seconds, range 780–1080. This plan selects from that packet. Unselected cards remain available in the original material.

The entry uses a documented reason for Snow's visit to the brewery: missing workers in the death list. The retrospective book frame allows household and Hampstead cases before the dated September action without assigning their acquisition to September 7. The closing follows the later physical investigation. Selection and order are editorial choices; the writer retains responsibility for the complete telling.

Validation: load the original packet as `EditorialInput`, load `plan.json` as `PlanningOutput`, then call `validate_against(material_locators=catalog(packet), speaker_ids=[speaker.label for speaker in packet.assignment.speakers])`. The five act ranges sum to 800–935 seconds; the working allocation sums to 900. Opening and ending are counted within their acts. Validation establishes structure and bindings, not literary quality.

No new provider call was made to prepare this control. No new comparison pairs were inspected. A live comparison and independent readings belong to the separately authorized experiment. Operator metadata in this README must stay outside model-facing plan fields.
