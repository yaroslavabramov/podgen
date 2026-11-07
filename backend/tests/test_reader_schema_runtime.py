"""Normal native/imported CLI readers pin schema semantics independently of projections."""

import json

import pytest

from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import service as service_module
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.reader_schema import FIELDS, VERSION
from podgen.writing.reader_schema import V1 as READER_V1
from tests.historical_revisions import historical_revision_manifest
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset


@pytest.mark.parametrize("source", ["native", "imported"])
@pytest.mark.parametrize("historical", [False, True, "v1"], ids=["new", "historical", "v1"])
def test_cli_reader_schema_preview_and_resume_keep_their_pinned_semantics(
    own_dataset, tmp_path, monkeypatch, source, historical
):
    if source == "native":
        prefix, _, options, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
        options["review_mode"] = "independent-readings/1"
    else:
        prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    if historical:
        original = EvaluationJournal.create_run

        def old_manifest(journal, command, manifest):
            manifest = historical_revision_manifest(manifest)
            if historical == "v1":
                manifest["reader_review_schema_version"] = READER_V1
            else:
                manifest.pop("reader_review_schema_version")
            for stage in manifest["response_schemas"]:
                if stage.startswith("reader-review"):
                    manifest["response_schemas"][stage] = response_schema(
                        stage,
                        command.input_contract,
                        reader_schema_version=READER_V1 if historical == "v1" else None,
                    )
            return original(journal, command, manifest)

        monkeypatch.setattr(EvaluationJournal, "create_run", old_manifest)
    provider = ReadingProvider(pause="reader-review")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert manifest.get("reader_review_schema_version") == (
        READER_V1 if historical == "v1" else None if historical else VERSION
    )
    pending = journal.request(journal.invocation(options["run_id"], "reader-review"))
    schema = pending["text"]["format"]["schema"]
    assert (schema["properties"]["listener_answer"]["description"] == FIELDS["listener_answer"]) == (
        historical is not True
    )
    assert ("revision_scope" in schema["properties"]) == (not historical)
    assert "assignment" not in payload(pending) and "editorial_frame" not in payload(pending)
    if not historical:
        script = dict(
            utterances=[
                {k: v for k, v in row.items() if k != "index"}
                for row in payload(pending)["script"]["utterances"]
            ]
        )
        path, preview = tmp_path / "actual-script.json", tmp_path / "preview.json"
        path.write_text(json.dumps(script, ensure_ascii=False))
        assert (
            invoke(
                prefix,
                "preview",
                run_id=options["run_id"],
                case="snow-pump",
                shared_ref="@" + str(tmp_path / "shared-ref.json"),
                stage="reader-review",
                profile=options["reviewer_profile"],
                draft=path,
                out=preview,
            )
            == 0
        )
        assert json.loads(preview.read_text())["request"] == pending
        count = len(provider.requests)

        def drift(stage, contract, **kwargs):
            schema = response_schema(stage, contract, **kwargs)
            if stage == "reader-review":
                schema["properties"]["listener_answer"]["description"] += " Incompatible change."
            return schema

        monkeypatch.setattr(service_module, "response_schema", drift)
        with pytest.raises(Conflict, match="role_output_contract_changed"):
            service_module.WritingEvaluation(own_dataset[0], lambda _: provider).run(
                WritingRunCommand.model_validate(manifest["command"])
            )
        assert len(provider.requests) == count
        monkeypatch.setattr(service_module, "response_schema", response_schema)
    monkeypatch.setattr(
        "podgen.writing.prompts.load_reading_instructions",
        lambda *a: pytest.fail("Resume must use pinned instructions"),
    )
    provider.pause = None
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "reader-review")
        == pending
    )
    assert journal.manifest(options["run_id"]) == manifest
    output = journal.output(journal.invocation(options["run_id"], "reader-review"))["output"]
    assert ("revision_scope" in output) == (not historical)
