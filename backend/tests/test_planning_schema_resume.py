"""Pending V5/V6 planner revisions resume pinned schemas; schema drift still fails closed."""

from copy import deepcopy

import pytest

from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import manifest as manifest_module
from podgen.runtime.evaluation import service as service_module
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences, bind_schema
from podgen.writing.projection_versions import CURRENT, V5, V6
from tests.test_card_ids import admitted, legacy
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import Paused
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_v5_recovery import RecoveryProvider


@pytest.mark.parametrize("version", [V5, V6])
def test_pending_planning_schema_is_pinned_and_drift_still_blocks_resume(
    own_dataset, tmp_path, monkeypatch, version
):
    prefix, _, options, _, data, _, _, _, _, _ = admitted(legacy(), own_dataset, tmp_path, monkeypatch)
    refs = ModelReferences.create(EditorialInput.model_validate(data))
    original = EvaluationJournal.create_run

    def frozen(journal, command, manifest):
        assert manifest["role_projection_version"] == CURRENT
        assert manifest["response_schemas"]["plan"] == bind_schema(
            response_schema("plan", command.input_contract, projection_version=CURRENT), refs
        )
        manifest = deepcopy(manifest)
        manifest.pop("reader_review_schema_version", None)
        manifest["role_projection_version"] = version
        manifest["response_schemas"] = {
            stage: bind_schema(
                response_schema(stage, command.input_contract, projection_version=version), refs
            )
            for stage in manifest["response_schemas"]
        }
        return original(journal, command, manifest)

    monkeypatch.setattr(EvaluationJournal, "create_run", frozen)
    provider = RecoveryProvider(refs, EditorialInput.model_validate(data), "pending")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    saved_manifest = journal.manifest(options["run_id"])
    assert "reader_review_schema_version" not in saved_manifest
    pending = journal.request(journal.invocation(options["run_id"], "plan-revise"))
    assert pending["text"]["format"]["schema"] == saved_manifest["response_schemas"]["plan-revise"]
    call_count = len(provider.requests)

    def drift(stage, contract, **kwargs):
        schema = response_schema(stage, contract, **kwargs)
        if stage == "plan":
            schema["$defs"]["PlanProposal"]["properties"]["working_answer"]["description"] = (
                "Deliberately incompatible schema text."
            )
        return schema

    monkeypatch.setattr(service_module, "response_schema", drift)
    command = WritingRunCommand.model_validate(saved_manifest["command"])
    with pytest.raises(Conflict, match="role_output_contract_changed"):
        service_module.WritingEvaluation(own_dataset[0], lambda _: provider).run(command)
    assert len(provider.requests) == call_count
    monkeypatch.setattr(service_module, "response_schema", response_schema)
    monkeypatch.setattr(
        manifest_module, "load_instruction_set", lambda *a, **k: pytest.fail("Resume must use pinned prompts")
    )
    provider.pause = None
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "plan-revise")
        == pending
    )
    assert journal.manifest(options["run_id"]) == saved_manifest
