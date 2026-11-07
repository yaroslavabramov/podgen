"""The ordinary CLI pins reference data once, including native preview and old manifests."""

import json

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation import manifest as manifest_module
from podgen.runtime.evaluation import preview as preview_module
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.test_craft_reference import reference_resource
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_script_handoff_runtime import EditingProvider


@pytest.mark.parametrize("historical", [False, True], ids=["new", "historical"])
def test_native_reference_is_immutable_in_run_preview_and_resume(
    own_dataset, tmp_path, monkeypatch, historical
):
    resource, raw = reference_resource(tmp_path, monkeypatch)
    prefix, _, options, _, ref = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options["review_mode"] = "independent-readings/1"
    if historical:
        original = EvaluationJournal.create_run

        def old(journal, command, manifest):
            return original(journal, command, {k: v for k, v in manifest.items() if k != "craft_reference"})

        monkeypatch.setattr(EvaluationJournal, "create_run", old)
    provider = EditingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert ("craft_reference" in manifest) != historical
    pending = journal.request(journal.invocation(options["run_id"], "revise-1"))
    resource.write_text("Совсем новый образец, не для этого запуска.")
    for module, name in ((preview_module, "load_reference"), (manifest_module, "load_reference")):
        monkeypatch.setattr(module, name, lambda *a: pytest.fail("Use the immutable reference"))
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
        assert payload(request).get("craft_reference") == (None if historical else raw.decode())
    for body in provider.requests:
        if body["metadata"]["podgen_stage"] != "draft":
            assert "craft_reference" not in payload(body)
    provider.pause = None
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
    monkeypatch.setattr(manifest_module, "load_reference", lambda *a: pytest.fail("No author role executes"))
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **options) == 0
    manifest = EvaluationJournal(own_dataset[0], own_dataset[1]).manifest(options["run_id"])
    assert "craft_reference" not in manifest
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "reader-review",
        "fact-review",
    ]
