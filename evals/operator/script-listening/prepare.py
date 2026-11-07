"""Compile ordinary, independent reader requests. The scoring key stays with the operator."""
import argparse
import json
from pathlib import Path
from random import SystemRandom
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.writing.editorial_contract import EditorialInput

HERE = Path(__file__).resolve().parent
PACKET = HERE.parents[1] / "editorial-inputs/current/snow-pump/shared.json"


def prepare(profile_path: Path, output: Path) -> None:
    profile = ModelProfile.model_validate_json(profile_path.read_bytes())
    cases = json.loads((HERE / "pairs.ru.json").read_text())["cases"]
    requests, key = [], {}
    for case in cases:
        packet = json.loads(PACKET.read_text())
        packet["assignment"].update(
            genre=case["genre"],
            duration={"min_seconds": 30, "target_seconds": 55, "max_seconds": 85},
        )
        material = EditorialInput.model_validate(packet)
        for variant, paragraphs in case["variants"].items():
            run_id = uuid4()
            shared = RuntimeRef(record_kind="user_input", record_id=run_id)
            materials = EditorialMaterials(material, shared, ())
            script = {"utterances": [{"speaker": "РАССКАЗЧИК", "text": p} for p in paragraphs]}
            body = preview_request(run_id, "reader-review", profile, materials, draft=script)
            data = json.loads(body["input"][0]["content"][0]["text"])
            if set(data) != {"script", "audience", "format", "genre", "output_language", "duration_context"}:
                raise ValueError("Review the cold-reader boundary before using these controls")
            requests.append({"id": str(run_id), "body": body})
            key[str(run_id)] = {"case": case["case"], "variant": variant}
    SystemRandom().shuffle(requests)
    output.mkdir(parents=True, exist_ok=False)
    (output / "requests.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests))
    (output / "operator-key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.profile, args.output)
