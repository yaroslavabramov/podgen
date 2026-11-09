"""Compile ordinary, independent reader requests. The scoring key stays with the operator."""

import argparse
import hashlib
import json
from pathlib import Path
from random import SystemRandom
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing.editorial_contract import EditorialInput

HERE = Path(__file__).resolve().parent
PACKET = HERE.parents[1] / "editorial-inputs/current/snow-pump/shared.json"
EPISODE = (
    HERE.parents[1] / "reports/2026-11-07-snow-short-editorial/after-reader-fix.md"
)
WRIGHT = HERE.parents[1] / "reports/2026-11-08-story-editing/transfer"


def wright_case():
    speech = (WRIGHT / "script.md").read_bytes()
    if (
        hashlib.sha256(speech).hexdigest()
        != "216e343a76f3ec926e3c596942e506db0cb5e2f641890fe71a45dd153caf3227"
    ):
        raise ValueError(
            "Reassess the Wright positive control before changing its text"
        )
    packet = (WRIGHT / "input/shared.json").read_bytes()
    assignment = json.loads(packet)["assignment"]
    return {
        "case": "Братья Райт — полный выпуск",
        "packet": packet,
        "genre": assignment["genre"],
        "duration": assignment["duration"],
        "variants": {"preserve_story": speech.decode().rstrip("\n").split("\n\n")},
    }


def prepare(
    profile_path: Path, output: Path, *, include_episode=False, include_wright=False
) -> None:
    profile = ModelProfile.model_validate_json(profile_path.read_bytes())
    cases = json.loads((HERE / "pairs.ru.json").read_text())["cases"]
    packet_bytes = PACKET.read_bytes()
    if include_episode:
        assignment = json.loads(packet_bytes)["assignment"]
        speech = EPISODE.read_bytes()
        if (
            hashlib.sha256(speech).hexdigest()
            != "bb79291ca6f6a3b33abc9ee2e05569cfda3422f9ba44e0eb7e274dcbde1b3a82"
        ):
            raise ValueError(
                "The reviewed episode has changed; verify its editorial label before comparing results"
            )
        cases.append(
            {
                "case": "Сноу — полный выпуск",
                "genre": assignment["genre"],
                "duration": assignment["duration"],
                "variants": {
                    "return": speech.decode("utf-8").rstrip("\n").split("\n\n")
                },
            }
        )
    if include_wright:
        cases.append(wright_case())
    requests, key = [], {}
    for case in cases:
        packet = json.loads(case.get("packet", packet_bytes))
        packet["assignment"].update(
            genre=case["genre"],
            duration=case.get(
                "duration", {"min_seconds": 30, "target_seconds": 55, "max_seconds": 85}
            ),
        )
        material = EditorialInput.model_validate(packet)
        for variant, paragraphs in case["variants"].items():
            run_id = uuid4()
            shared = RuntimeRef(record_kind="user_input", record_id=run_id)
            materials = EditorialMaterials(material, shared, ())
            script = {
                "utterances": [{"speaker": "РАССКАЗЧИК", "text": p} for p in paragraphs]
            }
            body = preview_request(
                run_id, "reader-review", profile, materials, draft=script
            )
            data = request_payload(body)
            if set(data) != {
                "script",
                "audience",
                "format",
                "genre",
                "output_language",
                "duration_context",
            }:
                raise ValueError(
                    "Review the cold-reader boundary before using these controls"
                )
            requests.append({"id": str(run_id), "body": body})
            key[str(run_id)] = {"case": case["case"], "variant": variant}
    SystemRandom().shuffle(requests)
    output.mkdir(parents=True, exist_ok=False)
    (output / "requests.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)
    )
    (output / "operator-key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--include-episode",
        action="store_true",
        help="Also prepare the full Snow negative control",
    )
    parser.add_argument(
        "--include-wright",
        action="store_true",
        help="Also prepare the full Wright positive control",
    )
    args = parser.parse_args()
    prepare(
        args.profile,
        args.output,
        include_episode=args.include_episode,
        include_wright=args.include_wright,
    )
