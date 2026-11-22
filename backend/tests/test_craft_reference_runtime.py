"""The ordinary CLI pins reference data once, including native preview and old manifests."""

import json

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing.behavior import BehaviorProfile
from tests.stage_pause import pause_before_count
from tests.test_craft_reference import reference_resource
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_script_handoff_runtime import EditingProvider


@pytest.mark.parametrize("mode", ["current", "shared_reference", "before_reference"])
def test_native_reference_is_immutable_in_run_preview_and_resume(own_dataset, tmp_path, monkeypatch, mode):
    resource, raw = reference_resource(tmp_path, monkeypatch)
    prefix, _, options, _, ref = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options["review_mode"] = "independent-readings/1"
    if mode != "current":
        original = EvaluationJournal.create_run

        def old(journal, command, manifest):
            manifest.pop("revision_context_contract")
            if mode == "before_reference":
                manifest.pop("craft_reference")
            return original(journal, command, manifest)

        monkeypatch.setattr(EvaluationJournal, "create_run", old)
    provider = EditingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "revise-1"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert ("craft_reference" in manifest) == (mode != "before_reference")
    assert ("revision_context_contract" in manifest) == (mode == "current")
    pending = journal.request(journal.invocation(options["run_id"], "revise-1"))
    resource.write_text("Совсем новый образец, не для этого запуска.")
    monkeypatch.setattr(BehaviorProfile, "craft", lambda *a: pytest.fail("Use the immutable reference"))
    script_files = {}
    for name in ("draft", "plan", "reader-review", "fact-review"):
        script_files[name] = tmp_path / (name + ".json")
        script_files[name].write_text(
            json.dumps(
                journal.output(journal.invocation(options["run_id"], name))["output"], ensure_ascii=False
            )
        )
    for stage in ("draft", "revise-1"):
        args = dict(approved_plan=script_files["plan"])
        if stage == "revise-1":
            args.update(
                draft=script_files["draft"],
                reader_feedback=script_files["reader-review"],
                fact_feedback=script_files["fact-review"],
            )
        preview = tmp_path / (stage + "-preview.json")
        assert (
            invoke(
                prefix,
                "preview",
                run_id=options["run_id"],
                case="snow-pump",
                shared_ref="@" + str(ref),
                stage=stage,
                profile=options["writer_profile"],
                out=preview,
                **args,
            )
            == 0
        )
        request = json.loads(preview.read_text())["request"]
        actual = (
            pending
            if stage == "revise-1"
            else next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "draft")
        )
        assert request == actual
        has_example = mode == "shared_reference" or (mode == "current" and stage == "draft")
        assert payload(request).get("craft_reference") == (raw.decode() if has_example else None)
        if stage == "revise-1":
            assert ("listener_understanding" in payload(request)["revision_guidance"][0]) == (
                mode != "current"
            )
    for body in provider.requests:
        if body["metadata"]["podgen_stage"] != "draft":
            assert "craft_reference" not in payload(body)

    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "revise-1") == pending
    )
    assert journal.manifest(options["run_id"]) == manifest


def test_read_only_imported_script_run_does_not_load_an_author_reference(own_dataset, tmp_path, monkeypatch):
    resource, _ = reference_resource(tmp_path, monkeypatch)
    resource.unlink()
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    options["max_script_revisions"] = 0
    monkeypatch.setattr(BehaviorProfile, "craft", lambda *a: pytest.fail("No author role executes"))
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 0
    manifest = EvaluationJournal(own_dataset[0], own_dataset[1]).manifest(options["run_id"])
    assert "craft_reference" not in manifest
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "reader-review",
        "fact-review",
    ]
