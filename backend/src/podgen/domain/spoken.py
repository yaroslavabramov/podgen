from .canonical import digest
from .contracts.common import ArtifactRef, LocalScriptSelector
from .contracts.writing import ScriptRevision, Utterance

ALGORITHM_VERSION = "spoken-view-v1"


def spoken_view(ref: ArtifactRef, script: ScriptRevision, count: int | None = None) -> dict:
    utterances = []
    for act in script.acts:
        for block in act.blocks:
            for item in block.items:
                if isinstance(item, Utterance):
                    selector = LocalScriptSelector(
                        act_id=act.act_id,
                        block_id=block.block_id,
                        item_id=item.utterance_id,
                        char_start=0,
                        char_end=len(item.text),
                    )
                    utterances.append(
                        dict(
                            selector=selector.model_dump(mode="json"),
                            speaker_id=str(item.speaker_id),
                            text=item.text,
                        )
                    )
    if count is not None:
        if count < 0 or count > len(utterances):
            raise ValueError("invalid_prefix_length")
        utterances = utterances[:count]
    data = dict(algorithm_version=ALGORITHM_VERSION, utterances=utterances)
    return dict(script_ref=ref.model_dump(mode="json"), view_hash=digest(data), **data)


def script_selection(script: ScriptRevision, selector: LocalScriptSelector) -> str:
    for act in script.acts:
        if act.act_id != selector.act_id:
            continue
        for block in act.blocks:
            if block.block_id != selector.block_id:
                continue
            for item in block.items:
                from .contracts.writing import item_id

                if item_id(item) != selector.item_id:
                    continue
                text = getattr(item, "text", getattr(item, "question", ""))
                if selector.char_start is None:
                    return text
                if selector.char_end > len(text):
                    raise ValueError("script_selector_out_of_bounds")
                return text[selector.char_start : selector.char_end]
    raise ValueError("script_item_not_found")
