"""Normal native/imported CLI readers pin schema semantics independently of projections."""

import json
from hashlib import sha256

import pytest

from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import service as service_module
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.behavior import BehaviorProfile
from podgen.writing.prompts import RoleInstructions
from podgen.writing.reader_schema import FIELDS, VERSION
from podgen.writing.reader_schema import V1 as READER_V1
from podgen.writing.reader_schema import V4 as READER_V4
from podgen.writing.reader_schema import V5 as READER_V5
from podgen.writing.reader_story import ORDER as STORY_ORDER
from podgen.writing.reader_wording import FIELDS as WORDING_FIELDS
from tests import FIXTURES
from tests.historical_revisions import historical_revision_manifest
from tests.stage_pause import pause_before_count
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset


@pytest.mark.parametrize("source", ["native", "imported"])
@pytest.mark.parametrize(
    "historical", [False, True, "v1", "v4", "v5"], ids=["new", "historical", "v1", "v4", "v5"]
)
def test_cli_reader_schema_preview_and_resume_keep_their_pinned_semantics(
    own_dataset, tmp_path, monkeypatch, source, historical
):
    reader_version = (
        VERSION if not historical else {"v1": READER_V1, "v4": READER_V4, "v5": READER_V5}.get(historical)
    )
    if historical == "v5":
        instructions_path = FIXTURES / "writing-instructions/reader-schema-legacy.md"
        old_body = instructions_path.read_text()

        released = BehaviorProfile.instructions

        def historical_prompt(profile, stage, format):
            if stage == "reader-review":
                return RoleInstructions(
                    "reader-review/sha256:" + sha256(old_body.encode()).hexdigest(), old_body
                )
            return released(profile, stage, format)

        monkeypatch.setattr(BehaviorProfile, "instructions", historical_prompt)
    if source == "native":
        prefix, _, options, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
        options["review_mode"] = "independent-readings/1"
    else:
        prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    if historical:
        original = EvaluationJournal.create_run

        def old_manifest(journal, command, manifest):
            manifest.pop("reading_audience")
            if historical not in ("v4", "v5"):
                manifest = historical_revision_manifest(manifest)
            if reader_version is None:
                manifest.pop("reader_review_schema_version")
            else:
                manifest["reader_review_schema_version"] = reader_version
            for stage in manifest["response_schemas"]:
                if stage.startswith("reader-review"):
                    manifest["response_schemas"][stage] = response_schema(
                        stage,
                        command.input_contract,
                        reader_schema_version=manifest.get("reader_review_schema_version"),
                    )
            return original(journal, command, manifest)

        monkeypatch.setattr(EvaluationJournal, "create_run", old_manifest)
    provider = ReadingProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pause_before_count(options["run_id"], "reader-review"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    manifest = journal.manifest(options["run_id"])
    assert manifest.get("reader_review_schema_version") == reader_version
    pending = journal.request(journal.invocation(options["run_id"], "reader-review"))
    schema = pending["text"]["format"]["schema"]
    assert (schema["properties"]["listener_answer"]["description"] == FIELDS["listener_answer"]) == (
        historical in ("v1", "v4")
    )
    if historical == "v5":
        assert pending["instructions"] == old_body and "## Worked edits" not in old_body
        assert schema["properties"]["listener_answer"]["description"] == WORDING_FIELDS["listener_answer"]
    if historical:
        assert payload(pending)["audience"] == "General listeners; no prior topic knowledge is assumed."
    else:
        assert schema["required"] == list(STORY_ORDER)
        assert payload(pending)["audience"] == manifest["reading_audience"]
        assert "## Worked edits" in pending["instructions"]
    scoped = not historical or historical in ("v4", "v5")
    assert ("revision_scope" in schema["properties"]) == scoped
    assert "assignment" not in payload(pending) and "editorial_frame" not in payload(pending)
    if not historical or historical in ("v4", "v5"):
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
        BehaviorProfile,
        "instructions",
        lambda *a: pytest.fail("Resume must use pinned instructions"),
    )

    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "reader-review")
        == pending
    )
    assert journal.manifest(options["run_id"]) == manifest
    output = journal.output(journal.invocation(options["run_id"], "reader-review"))["output"]
    assert ("revision_scope" in output) == scoped
