"""Prepare ordinary plan-review requests and a separate human key; never call a provider."""

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.wire_budget import check_visible
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from podgen.writing.projection_versions import CURRENT
from podgen.writing.projections import CONTRACT
from podgen.writing.prompts import load_instruction_set


def replace(document, pointer, value):
    tokens = pointer.split("/")[1:]
    target = document
    for token in tokens[:-1]:
        target = target[int(token)] if isinstance(target, list) else target[token]
    assert tokens[-1] in target
    target[tokens[-1]] = value


def prepare(profile_path, output):
    root = Path(__file__).resolve().parents[3]
    suite = json.loads(Path(__file__).with_name("review-controls.json").read_text())
    for binding in (suite["input"], suite["base_plan"]):
        assert hashlib.sha256((root / binding["path"]).read_bytes()).hexdigest() == binding["sha256"]
    packet = json.loads((root / suite["input"]["path"]).read_text())
    profile = ModelProfile.model_validate_json(profile_path.read_bytes())
    base = json.loads((root / suite["base_plan"]["path"]).read_text())
    for pointer, value in suite["common_replacements"].items():
        replace(base, pointer, value)
    prompt = load_instruction_set("solo", include_planning=True)["plan-review"]
    requests, key = [], []
    for case in suite["pair"] + suite["cases"]:
        data, candidate = deepcopy(packet), deepcopy(base)
        if case["client_condition"]:
            data["assignment"]["style_guidance"].append(case["client_condition"])
        for pointer, value in case["patches"].items():
            replace(candidate, pointer, value)
        parsed = EditorialInput.model_validate(data)
        refs = ModelReferences.create(parsed)
        run_id = uuid5(NAMESPACE_URL, "podgen/plan-review-control/1/" + case["id"])
        materials = EditorialMaterials(parsed, RuntimeRef(record_kind="user_input", record_id=run_id), ())
        body = preview_request(
            run_id, "plan-review", profile, materials, candidate=refs.translate(candidate, inbound=True)
        )
        check_visible(body, CONTRACT)
        visible = json.loads(body["input"][0]["content"][0]["text"])
        assert body["instructions"] == prompt.body
        assert visible["dossier"]["knowledge_cards"] and visible["evidence"]
        assert case["focus"] in visible["allowed_review_targets"]
        if "anchor" in case["expected"]:
            assert case["expected"]["anchor"] in value_at(candidate, case["focus"])
        for check in case["expected"].get("checks", []):
            assert check["anchor"] in value_at(candidate, check["target"])
        assert set(visible) == {
            "output_language",
            "assignment",
            "dossier",
            "material_catalog",
            "evidence",
            "candidate_plan",
            "review_target_root",
            "allowed_review_targets",
        }
        requests.append(dict(custom_id=case["id"], body=body))
        key.append(
            dict(
                id=case["id"],
                focus=case["focus"],
                fragment=value_at(candidate, case["focus"]),
                expected=case["expected"],
                origin=case["origin"],
                client_condition=case["client_condition"],
            )
        )
    pair = requests[:2]
    left, right = [json.loads(row["body"]["input"][0]["content"][0]["text"]) for row in pair]
    assert {k: v for k, v in left.items() if k != "candidate_plan"} == {
        k: v for k, v in right.items() if k != "candidate_plan"
    }
    assert {k: v for k, v in pair[0]["body"].items() if k not in ("input", "metadata")} == {
        k: v for k, v in pair[1]["body"].items() if k not in ("input", "metadata")
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "requests.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pair)
    )
    (output / "offline-requests.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in requests[2:])
    )
    save(output / "manual-key.json", dict(provenance=suite["provenance"], cases=key))
    save(
        output / "manifest.json",
        dict(
            input=suite["input"],
            base_plan=suite["base_plan"],
            common_replacements=suite["common_replacements"],
            profile_sha256=sha(profile_path.read_bytes()),
            model=profile.model_revision_or_alias,
            projection=CURRENT,
            prompt_version=prompt.version,
            model_called=False,
            schema_sha256=sha(
                json.dumps(pair[0]["body"]["text"]["format"]["schema"], separators=(",", ":")).encode()
            ),
            request_sha256={
                row["custom_id"]: sha(
                    json.dumps(row["body"], ensure_ascii=False, separators=(",", ":")).encode()
                )
                for row in requests
            },
        ),
    )
    return output


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def value_at(document, pointer):
    for token in pointer.split("/")[1:]:
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, type=Path, help="Complete admitted ModelProfile JSON; no API key."
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="New output directory; existing files are never overwritten."
    )
    args = parser.parse_args()
    print(prepare(args.profile, args.out))
