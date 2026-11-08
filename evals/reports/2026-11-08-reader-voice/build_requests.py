"""Compile blind, isolated native reader requests; no provider is constructed."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.evaluation.wire_budget import check_visible, visible_sizes
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projection_versions import V8
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import VERSION
from podgen.writing.reading_views import reading_payload

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists():
        assert path.read_bytes() == data, path
    else:
        path.write_bytes(data)


def compile_requests(version="v1"):
    frozen = read(HERE / "frozen.json")
    archive = read(HERE / "archive/request.json")
    command = read(ROOT / "tmp/snow-edit-nov7/continued-native/export/manifest.json")[
        "command"
    ]
    packet = EditorialInput.model_validate_json(
        (
            ROOT
            / "tmp/snow-edit-nov7/continued-native/export/editorial-input-original.json"
        ).read_bytes()
    )
    materials = EditorialMaterials(
        packet, RuntimeRef.model_validate(command["author_ref"]), ()
    )
    assert (
        response_schema(
            "reader-review-2",
            "editorial-role-packets/1",
            projection_version=V8,
            reader_schema_version=VERSION,
        )
        == archive["text"]["format"]["schema"]
    )
    old = (HERE / "old-reader.md").read_text()
    new = load_reading_instructions("reader-review")
    assert sha256(old.encode()).hexdigest() == frozen["old_prompt_sha256"]
    assert not any("\u0400" <= c <= "\u04ff" for c in new.body)
    assert "Snow" not in new.body and "83" not in new.body
    new_path = HERE / f"new-reader-{version}.md"
    if new_path.exists():
        assert new_path.read_text() == new.body
    else:
        new_path.write_text(new.body)
    rows = []
    for case, expected in frozen["case_sha256"].items():
        script_path = HERE / "texts" / f"{case}.json"
        assert sha256(script_path.read_bytes()).hexdigest() == expected
        script = read(script_path)
        selected = materials
        if case == "05":
            assignment = materials.packet.assignment
            assignment = assignment.model_copy(
                update={
                    "genre": "Короткий бытовой рассказ",
                    "duration": assignment.duration.__class__(
                        target_seconds=90, min_seconds=60, max_seconds=120
                    ),
                }
            )
            selected = replace(
                materials,
                packet=materials.packet.model_copy(update={"assignment": assignment}),
            )
        built = {}
        for role, instructions in (("old", old), (f"new-{version}", new.body)):
            identifier = uuid5(
                NAMESPACE_URL, "podgen-reader-voice:" + role + ":" + case
            )
            body = deepcopy(archive)
            body["input"][0]["content"][0]["text"] = json.dumps(
                reading_payload(
                    "reader-review", selected, script=script, projection_version=V8
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            body["instructions"] = instructions
            body["metadata"]["podgen_run_id"] = str(identifier)
            body["metadata"]["podgen_invocation_id"] = str(
                uuid5(identifier, "invocation:reader-review-2")
            )
            assert body["model"] == "gpt-6.1-terra" and body["reasoning"] == {
                "effort": "medium"
            }
            assert body["max_output_tokens"] == archive["max_output_tokens"] == 24576
            assert body["text"] == archive["text"]
            assert body["tools"] == [] and body["tool_choice"] == "none"
            visible = json.loads(body["input"][0]["content"][0]["text"])
            assert set(visible) == set(
                json.loads(archive["input"][0]["content"][0]["text"])
            )
            assert [
                {k: v for k, v in u.items() if k != "index"}
                for u in visible["script"]["utterances"]
            ] == script["utterances"]
            assert not set(visible) & {
                "assignment",
                "editorial_frame",
                "support",
                "evidence",
                "craft_reference",
                "criteria",
                "case",
                "expected",
            }
            check_visible(body, "editorial-role-packets/1")
            built[role] = body
            if role == "old" and case == "01":
                assert visible == json.loads(archive["input"][0]["content"][0]["text"])
                assert instructions == archive["instructions"]
                body = deepcopy(archive)
            directory = HERE / "runs" / role / case
            write(directory / "request.json", body)
            write(
                directory / "call.json",
                {
                    "role": role,
                    "case": case,
                    "endpoint_called": False,
                    "reuse_archival": role == "old" and case == "01",
                    "prompt_cid": (
                        "reader-review/sha256:"
                        + sha256(instructions.encode()).hexdigest()
                    ),
                    "visible_bytes": visible_sizes(body),
                },
            )
            rows.append(
                {
                    "role": role,
                    "case": case,
                    "path": str((directory / "request.json").relative_to(ROOT)),
                }
            )
        a, b = (deepcopy(built[k]) for k in ("old", f"new-{version}"))
        for item in (a, b):
            item.pop("instructions")
            item.pop("metadata")
        assert a == b
    write(
        HERE / f"requests-{version}.json",
        {
            "new_role_cid": new.version,
            "requests": rows,
            "model": "gpt-6.1-terra",
            "reasoning": "medium",
            "new_calls": 9,
        },
    )
    print(
        json.dumps(
            {
                "new_role_cid": new.version,
                "prepared": len(rows),
                "new_calls": 9,
                "output_limit": 24576,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    compile_requests()
