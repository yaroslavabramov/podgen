"""Freeze all excerpt requests and operator criteria before the first live call."""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from run_readings import HERE, read
from speech_contract import response_schema


def freeze(path, value):
    from build_requests import write

    write(path, value)


def main():
    prompt = (HERE / "speech-proofread.md").read_text()
    assert not any("\u0400" <= char <= "\u04ff" for char in prompt)
    assert "Snow" not in prompt and "eighty" not in prompt
    source = read(HERE / "archive/request.json")
    rows = []
    for case in ("01", "02", "03", "04", "05"):
        text = read(HERE / "texts" / f"{case}.json")
        utterances = text["utterances"] if case == "05" else text["utterances"][:4]
        payload = {
            "output_language": "ru",
            "utterances": [
                dict(index=index, **row) for index, row in enumerate(utterances)
            ],
        }
        identifier = uuid5(NAMESPACE_URL, "podgen-independent-speech-v1:" + case)
        request = deepcopy(source)
        request.update(
            instructions=prompt,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                payload, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                    ],
                }
            ],
            text={
                "verbosity": "high",
                "format": {
                    "type": "json_schema",
                    "name": "speech_proofread",
                    "schema": response_schema(),
                    "strict": True,
                },
            },
        )
        request["metadata"]["podgen_run_id"] = str(identifier)
        request["metadata"]["podgen_invocation_id"] = str(
            uuid5(identifier, "speech-proofread")
        )
        directory = HERE / "runs/speech-v1" / case
        freeze(directory / "request.json", request)
        rows.append(
            {
                "case": case,
                "request_sha256": sha256(
                    (directory / "request.json").read_bytes()
                ).hexdigest(),
            }
        )
    criteria = {
        "operator_only": True,
        "frozen_before_calls": True,
        "hypothesis": "An independent consecutive-excerpt language pass without summary or story-design assessment detects grammar and cumulative voice separately.",
        "inputs": "01-04 first four consecutive utterances; 05 complete unrelated three-utterance story. Exactly the previously frozen words; original indices retained. No author A/B samples, votes, criteria or previous reviews in requests.",
        "initial_cases": ["01", "04", "05"],
        "followup_cases_only_if_initial_pass": ["02", "03"],
        "initial_success": {
            "01": "Both a required exact case correction and a required concrete passage/wording repair grounded in neighboring stylized opening phrases, preserving the inspection-to-interviews transition. Grammar-only is failure. Praise or a vague style label is not repair.",
            "04": "No invented grammar defect or required opening style rewrite; optional justified copyediting allowed. Useful image and transition remain allowed.",
            "05": "No invented grammar defect or required style rewrite; the train-window metaphor is not treated as intrinsically defective. Empty findings is valid.",
        },
        "followup_success": {
            "02": "Required cumulative wording repair despite corrected grammar; no accusation about corrected numeral.",
            "03": "Required correction of the retained case error; no required cumulative rewrite of the already edited opening.",
        },
        "stop": "At most three initial calls and two conditional calls; no prompt iteration or automatic retries. If initial criteria fail, save the failure and stop. All calls use Terra medium, strict schema, identical prompt and 24576 max output tokens.",
        "requests": rows,
        "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        "schema": response_schema(),
    }
    freeze(HERE / "speech-criteria.json", criteria)


if __name__ == "__main__":
    main()
