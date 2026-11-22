# Select passages for unresolved anchors

For every offered invalid anchor, select the supplied passage that supports its existing editorial judgment. Copy `anchor_id` and `selector_id` exactly. Choose `selector_id=null` if none fits. Return one choice per offered anchor, without omissions or duplicates.

This task only locates passages. Keep judgments, evidence and already valid anchors unchanged; do not rewrite or approve the script. Return only the requested choices.
