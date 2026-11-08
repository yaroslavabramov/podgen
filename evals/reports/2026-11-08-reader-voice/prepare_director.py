"""Freeze the final instruction-only contrast before its three calls."""

from copy import deepcopy
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from build_requests import write
from run_readings import HERE, read


def main():
    prompt = (HERE / "speech-director.md").read_text()
    assert not any("\u0400" <= char <= "\u04ff" for char in prompt)
    assert "Snow" not in prompt and "court" not in prompt
    rows = []
    for case in ("01", "04", "05"):
        prior = read(HERE / "runs/speech-v1" / case / "request.json")
        request = deepcopy(prior)
        request["instructions"] = prompt
        identifier = uuid5(NAMESPACE_URL, "podgen-speech-director-v2:" + case)
        request["metadata"]["podgen_run_id"] = str(identifier)
        request["metadata"]["podgen_invocation_id"] = str(
            uuid5(identifier, "speech-proofread")
        )
        directory = HERE / "runs/speech-v2" / case
        write(directory / "request.json", request)
        for item in (prior, request):
            item.pop("instructions")
            item.pop("metadata")
        assert prior == request
        rows.append(
            {
                "case": case,
                "request_sha256": sha256(
                    (directory / "request.json").read_bytes()
                ).hexdigest(),
            }
        )
    write(
        HERE / "director-criteria.json",
        {
            "operator_only": True,
            "frozen_before_calls": True,
            "hypothesis": "A director's counterfactual removal test identifies cumulative narrator self-commentary missed by general language checking.",
            "comparison": "Only instructions differ from speech-v1; metadata UUIDs renewed. Identical excerpts, schema, model, reasoning, output budget and provider settings. No previous results or operator expectations supplied to the model.",
            "initial_success": read(HERE / "speech-criteria.json")["initial_success"],
            "stop": "Exactly three calls: 01, 04, 05. Both required grammar and concrete cumulative wording repair on 01, with the useful transition preserved; clean 04 and metaphor 05 must have no invented required repair. If any criterion fails, conclude this line without further attempts or production integration.",
            "prompt_sha256": sha256(prompt.encode()).hexdigest(),
            "schema": read(HERE / "speech-criteria.json")["schema"],
            "requests": rows,
        },
    )


if __name__ == "__main__":
    main()
