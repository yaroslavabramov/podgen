"""Only the unresolved addresses and their exact selectable targets enter repair calls."""

from copy import deepcopy


def replace_input(body, instructions, payload):
    import json

    from podgen.writing.projections import validate_model_content

    validate_model_content(
        dict(instructions=instructions, input=payload, schema=body["text"]["format"]["schema"])
    )
    body["instructions"] = instructions
    body["input"] = [
        dict(
            role="user",
            content=[
                dict(
                    type="input_text",
                    text=json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                )
            ],
        )
    ]


def anchor_payload(plan):
    original = plan.as_payload()
    selected = set()
    for broken, row in zip(plan.broken, original["invalid_anchors"], strict=True):
        anchor = plan.feedback.anchored_groups()[broken.group][broken.entry].anchors[broken.anchor]
        indices = {i for i, utterance in enumerate(plan.draft.utterances) if anchor.quote in utterance.text}
        if anchor.utterance_index < len(plan.draft.utterances):
            indices.add(anchor.utterance_index)
        # Offer exact complete/sentence ranges only in addressable relevant utterances.
        choices = [passage.selector_id for passage in plan.passages if passage.utterance_index in indices]
        row["selector_ids"] = choices
        selected.update(choices)
    return dict(
        invalid_anchors=original["invalid_anchors"],
        selectors=[row for row in original["selectors"] if row["selector_id"] in selected],
    )


def target_payload(plan):
    # The complete candidate occurs once; its address catalog contains no repeated field values.
    return dict(candidate_plan=plan.candidate.model_dump(mode="json"), **deepcopy(plan.as_payload()))
