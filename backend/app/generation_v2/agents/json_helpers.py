from __future__ import annotations

import json


def json_for_prompt(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value],
            ensure_ascii=False,
        )
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)
