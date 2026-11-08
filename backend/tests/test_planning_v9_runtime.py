"""Native V9 and pinned V8 planning requests, previews and resume without writer inference."""

import json
from copy import deepcopy

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.plan_views import candidate_view
from podgen.writing.projection_versions import V8, V9
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset


@pytest.mark.parametrize("version", [V8, V9])
def test_native_planning_snapshot_and_resume_keep_the_exact_wire(own_dataset, tmp_path, monkeypatch, version):
    prefix, _, options, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options["review_mode"] = "independent-readings/1"
    if version == V8:
        original = EvaluationJournal.create_run

        def pinned(journal, command, manifest):
            manifest = deepcopy(manifest)
            manifest["role_projection_version"] = V8
            manifest.pop("writer_response_contract", None)
            for stage in manifest["response_schemas"]:
                manifest["response_schemas"][stage] = response_schema(
                    stage,
                    command.input_contract,
                    projection_version=V8,
                    reader_schema_version=manifest.get("reader_review_schema_version"),
                    fact_schema_version=manifest.get("fact_review_schema_version"),
                )
            return original(journal, command, manifest)

        monkeypatch.setattr(EvaluationJournal, "create_run", pinned)
    provider = ReadingProvider(pause="plan-review")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    candidate = journal.output(journal.invocation(options["run_id"], "plan"))["output"]
    assert ("last_moment" in candidate["plan"]["ending"]) == (version == V9)
    pending = journal.request(journal.invocation(options["run_id"], "plan-review"))
    source = tmp_path / "candidate.json"
    source.write_text(json.dumps(candidate, ensure_ascii=False))
    preview = tmp_path / "preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(tmp_path / "shared-ref.json"),
            stage="plan-review",
            profile=options["plan_reviewer_profile"],
            candidate=source,
            out=preview,
        )
        == 0
    )
    assert json.loads(preview.read_text())["request"] == pending
    assert (
        payload(pending)["candidate_plan"]["plan"]["ending"]
        == candidate_view(candidate, projection_version=version)["plan"]["ending"]
    )
    provider.pause = "episode-script"
    # The existing provider pauses by the counted format name, before author generation.
    with pytest.raises(Paused):
        invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export")
    assert len([body for body in provider.requests if body["metadata"]["podgen_stage"] == "plan"]) == 1
    assert journal.manifest(options["run_id"]) == manifest
