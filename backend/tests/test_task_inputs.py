from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.runtime import (
    InputAdmissionRecord,
    InputManifest,
    TaskResult,
    TaskResultRecord,
    TaskRun,
    TaskSpec,
)
from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.task_inputs import TaskInputs
from tests.factories import Factory


def test_new_source_and_search_metadata_have_exact_immutable_manifest_chain(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    f.spec = f.make("EpisodeSpec", f.spec.payload | {"source_mode": "open_web"})
    prompt = f.make(
        "PromptDefinition",
        dict(
            role="writer",
            template="ТЕСТОВАЯ задача: объясни часы GPS",
            placeholders=[],
            examples=[],
            compiler_version="test-v1",
        ),
    )
    tools = f.make("ToolsetDefinition", dict(tools=[], dispatcher_version="test-v1"))
    task_id, manifest_id, run_id = uuid4(), uuid4(), uuid4()
    manifest = InputManifest(
        manifest_id=manifest_id,
        parent_manifest_id=None,
        project_id=f.project,
        task_id=task_id,
        revision_refs=[f.spec.ref],
        runtime_refs=[],
        source_scope=dict(
            mode="open_web",
            source_policy_ref=f.policies["source"],
            authorized_input_ids=[],
            authorized_source_refs=[],
            network_rule_id=None,
            external_processing_rule_id="test-only",
        ),
        policy_ref=f.bundle.ref,
        prompt_ref=prompt.ref,
        toolset_ref=tools.ref,
        model_profile_ref=f.model.ref,
    )
    task = TaskSpec(
        task_id=task_id,
        role="writer",
        objective="ТЕСТОВЫЙ диалог о часах GPS",
        why_it_matters="Ответ на вопрос",
        input_manifest_id=manifest_id,
        acceptance_questions=["Понятна ли четвёртая неизвестная?"],
        allowed_actions=[],
        output_kind="ScriptRevision",
        unresolved_context=[],
        operational_limits=dict(max_model_calls=2, max_tool_calls=4, max_elapsed_seconds=60),
    )
    inputs = TaskInputs(repo)
    inputs.create_task(f.project, task, manifest)
    run = TaskRun(
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        episode_run_id=uuid4(),
        purpose="evaluation",
        generation_epoch=0,
        current_manifest_id=manifest_id,
        checkpoints=[],
        status="running",
    )
    inputs.register_run(f.project, run)
    discovery = RuntimeRef(record_kind="search_record", record_id=uuid4())
    repo.append_runtime(
        f.project, discovery, dict(test_data=True, snippet="ТЕСТ: только metadata, ещё не evidence")
    )
    source, _ = f.source()
    next_ = manifest.model_copy(
        update=dict(
            manifest_id=uuid4(),
            parent_manifest_id=manifest_id,
            revision_refs=[f.spec.ref, source.ref],
            runtime_refs=[discovery],
        )
    )
    admission = InputAdmissionRecord(
        admission_id=uuid4(),
        project_id=f.project,
        task_run_id=run_id,
        parent_manifest_id=manifest_id,
        next_manifest_id=next_.manifest_id,
        admitted_refs=[source.ref],
        admitted_runtime_refs=[discovery],
        rejected_candidates=[],
        origin="tool_result",
        origin_id=uuid4(),
        policy_ref=f.bundle.ref,
        scope_decision="ТЕСТ: источник разрешён",
        created_at="2026-11-01T00:00:00Z",
    )
    inputs.admit(f.project, next_, admission)
    inputs.admit(f.project, next_, admission)  # lost acknowledgement
    second_run = run.model_copy(update={"run_id": uuid4()})
    inputs.register_run(f.project, second_run)
    third = next_.model_copy(update={"manifest_id": uuid4(), "parent_manifest_id": next_.manifest_id})
    borrowed = admission.model_copy(
        update={
            "admission_id": uuid4(),
            "task_run_id": second_run.run_id,
            "parent_manifest_id": next_.manifest_id,
            "next_manifest_id": third.manifest_id,
            "admitted_refs": [],
            "admitted_runtime_refs": [],
        }
    )
    with pytest.raises(Conflict, match="manifest_parent_not_current_for_run"):
        inputs.admit(f.project, third, borrowed)
    for field, ref in [
        (
            "prompt_ref",
            f.make("PromptDefinition", prompt.payload | {"template": "SYNTHETIC changed prompt"}).ref,
        ),
        ("toolset_ref", f.make("ToolsetDefinition", tools.payload | {"dispatcher_version": "changed"}).ref),
        ("model_profile_ref", f.make("ModelProfile", f.model.payload | {"model_id": "changed"}).ref),
    ]:
        changed = third.model_copy(update={field: ref})
        change_admission = borrowed.model_copy(update={"admission_id": uuid4(), "task_run_id": run.run_id})
        with pytest.raises(ContractError, match="manifest_admission_binding_mismatch"):
            inputs.admit(f.project, changed, change_admission)

    assert inputs.manifest(f.project, manifest_id) == manifest
    assert inputs.manifest(f.project, next_.manifest_id) == next_
    with pytest.raises(ContractError, match="admission_id_reused_with_different_input"):
        inputs.admit(f.project, next_, admission.model_copy(update={"admitted_refs": []}))
    result = TaskResultRecord(
        result_id=uuid4(),
        run_id=run_id,
        payload=TaskResult(
            task_id=task_id,
            status="partial",
            output_refs=[source.ref],
            remaining_questions=["Нужно написать диалог"],
            next_action="Продолжить",
            failure=None,
        ),
    )
    inputs.save_result(f.project, result)
    inputs.save_result(f.project, result)
    with pytest.raises(Conflict, match="already_has_final_result"):
        inputs.save_result(f.project, result.model_copy(update={"result_id": uuid4()}))
