"""Immutable task inputs and manifest admissions; delivery receipts belong to WP-03."""

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.contracts.runtime import InputManifest, TaskRun, TaskSpec
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import check_subtype, walk_values

from .database import json_text, one, query

ROLE_OUTPUTS = {
    "researcher": {"ResearchNote"},
    "editor": {"EditorialDecision"},
    "writer": {"ScriptRevision"},
    "factchecker": {"VerificationReport"},
    "editorial_reviewer": {"EditorialReview"},
    "listener_state": {"ListenerState"},
    "director": {"PerformancePlan"},
    "audio_reviewer": {"AudioQAReport"},
}


class TaskInputs:
    def __init__(self, repository):
        self.repo = repository

    def _refs(self, tx, project, value):
        for field, _, ref in walk_values(value):
            if isinstance(ref, ArtifactRef):
                row = self.repo._resolve(tx, project, ref)
                check_subtype(field, row["payload"])
            elif isinstance(ref, RuntimeRef):
                self.repo._runtime(tx, project, ref)
            else:
                self.repo._check_blob(tx, project, ref)

    def _append(self, tx, project, kind, id, payload, parent=None):
        existing = one(
            tx,
            "SELECT * FROM operational_records WHERE project_id=:p AND record_kind=:k AND record_id=:id",
            p=project,
            k=kind,
            id=id,
        )
        if existing:
            if existing["payload"] != payload:
                raise Conflict("runtime_id_reused")
            return
        self._refs(tx, project, payload)
        query(
            tx,
            "INSERT INTO operational_records VALUES(:p,:k,:id,:parent,CAST(:body AS json))",
            p=project,
            k=kind,
            id=id,
            parent=parent,
            body=json_text(payload),
        )

    def _get(self, tx, project, kind, id):
        self.repo._acl(tx, project)
        row = one(
            tx,
            "SELECT payload FROM operational_records WHERE project_id=:p AND record_kind=:k AND record_id=:id",
            p=project,
            k=kind,
            id=id,
        )
        if not row:
            raise AccessDenied()
        self._refs(tx, project, row["payload"])
        return row["payload"]

    def create_task(self, project, task: TaskSpec, manifest: InputManifest):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project, write=True)
            repo._lock(tx, project)
            if task.role not in ROLE_OUTPUTS or task.output_kind not in ROLE_OUTPUTS[task.role]:
                raise ContractError("role_output_kind_mismatch")
            if (
                manifest.project_id != project
                or manifest.task_id != task.task_id
                or task.input_manifest_id != manifest.manifest_id
                or manifest.parent_manifest_id is not None
            ):
                raise ContractError("initial_task_manifest_binding_mismatch")
            self._validate_manifest(tx, project, manifest)
            profile = repo._resolve(tx, project, manifest.model_profile_ref)["payload"]
            prompt = repo._resolve(tx, project, manifest.prompt_ref)["payload"]
            tools = repo._resolve(tx, project, manifest.toolset_ref)["payload"]
            if (
                task.role not in profile["allowed_roles"]
                or prompt["role"] != task.role
                or set(task.allowed_actions) - {t["name"] for t in tools["tools"]}
            ):
                raise ContractError("task_configuration_role_or_tools_mismatch")
            self._append(tx, project, "task_spec", task.task_id, task.model_dump(mode="json"))
            self._append(
                tx, project, "input_manifest", manifest.manifest_id, manifest.model_dump(mode="json")
            )

    def _validate_manifest(self, tx, project, manifest):
        self._refs(tx, project, manifest.model_dump(mode="json"))
        bundle = self.repo._resolve(tx, project, manifest.policy_ref)
        if (
            manifest.source_scope.source_policy_ref.model_dump(mode="json")
            != bundle["payload"]["source_policy_ref"]
        ):
            raise ContractError("manifest_source_policy_mismatch")
        for ref in manifest.revision_refs:
            if ref.kind == "EpisodeSpec":
                spec = self.repo._resolve(tx, project, ref)["payload"]
                if spec["source_mode"] != manifest.source_scope.mode or spec[
                    "source_policy_ref"
                ] != manifest.source_scope.source_policy_ref.model_dump(mode="json"):
                    raise ContractError("manifest_episode_source_scope_mismatch")
        kinds = [
            (manifest.policy_ref, "PolicyBundle"),
            (manifest.prompt_ref, "PromptDefinition"),
            (manifest.toolset_ref, "ToolsetDefinition"),
            (manifest.model_profile_ref, "ModelProfile"),
        ]
        if any(ref.kind != kind for ref, kind in kinds):
            raise ContractError("manifest_config_kind_mismatch")

    def register_run(self, project, run: TaskRun):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project, write=True)
            task = self._get(tx, project, "task_spec", run.task_id)
            if run.current_manifest_id != TaskSpec.model_validate(task).input_manifest_id:
                raise ContractError("run_initial_manifest_mismatch")
            if run.purpose == "evaluation" and repo.purpose != "evaluation":
                raise AccessDenied()
            self._append(tx, project, "task_run", run.run_id, run.model_dump(mode="json"))
            query(
                tx,
                "INSERT INTO task_input_heads(project_id,run_id,task_id,manifest_id) VALUES(:p,:r,:t,:m) ON CONFLICT DO NOTHING",
                p=project,
                r=run.run_id,
                t=run.task_id,
                m=run.current_manifest_id,
            )

    def admit(self, project, next_manifest, admission):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project, write=True)
            repo._lock(tx, project)
            run = TaskRun.model_validate(self._get(tx, project, "task_run", admission.task_run_id))
            existing = one(
                tx,
                "SELECT payload FROM operational_records WHERE project_id=:p AND record_kind='input_admission' AND record_id=:id",
                p=project,
                id=admission.admission_id,
            )
            if existing:
                if existing["payload"] != admission.model_dump(mode="json"):
                    raise Conflict("admission_id_reused_with_different_input")
                stored = InputManifest.model_validate(
                    self._get(tx, project, "input_manifest", admission.next_manifest_id)
                )
                if stored != next_manifest:
                    raise Conflict("manifest_id_reused_with_different_input")
                return
            head = one(
                tx,
                "SELECT * FROM task_input_heads WHERE project_id=:p AND run_id=:r FOR UPDATE",
                p=project,
                r=run.run_id,
            )
            if not head or head["manifest_id"] != admission.parent_manifest_id:
                raise Conflict("manifest_parent_not_current_for_run")
            parent = InputManifest.model_validate(
                self._get(tx, project, "input_manifest", admission.parent_manifest_id)
            )
            if (
                admission.project_id != project
                or next_manifest.project_id != project
                or next_manifest.parent_manifest_id != parent.manifest_id
                or admission.next_manifest_id != next_manifest.manifest_id
                or parent.task_id != next_manifest.task_id
                or parent.task_id != run.task_id
                or next_manifest.policy_ref != parent.policy_ref
                or admission.policy_ref != parent.policy_ref
                or next_manifest.source_scope != parent.source_scope
                or next_manifest.prompt_ref != parent.prompt_ref
                or next_manifest.toolset_ref != parent.toolset_ref
                or next_manifest.model_profile_ref != parent.model_profile_ref
            ):
                raise ContractError("manifest_admission_binding_mismatch")

            def key(ref):
                return json_text(ref)

            old = {key(r) for r in parent.revision_refs}
            new = {key(r) for r in next_manifest.revision_refs}
            old_runtime = {key(r) for r in parent.runtime_refs}
            new_runtime = {key(r) for r in next_manifest.runtime_refs}
            if (
                not old <= new
                or new - old != {key(r) for r in admission.admitted_refs}
                or not old_runtime <= new_runtime
                or new_runtime - old_runtime != {key(r) for r in admission.admitted_runtime_refs}
            ):
                raise ContractError("manifest_admission_delta_mismatch")
            previous = query(
                tx,
                "SELECT payload FROM operational_records WHERE project_id=:p AND record_kind='input_admission' AND parent_record_id=:r",
                p=project,
                r=run.run_id,
            ).mappings()
            for old_admission in previous:
                body = old_admission["payload"]
                if body["parent_manifest_id"] == str(parent.manifest_id) and body["next_manifest_id"] != str(
                    next_manifest.manifest_id
                ):
                    raise Conflict("manifest_chain_fork")
            self._validate_manifest(tx, project, next_manifest)
            self._append(
                tx,
                project,
                "input_manifest",
                next_manifest.manifest_id,
                next_manifest.model_dump(mode="json"),
                parent.manifest_id,
            )
            self._append(
                tx,
                project,
                "input_admission",
                admission.admission_id,
                admission.model_dump(mode="json"),
                run.run_id,
            )
            query(
                tx,
                "UPDATE task_input_heads SET manifest_id=:m,lock_version=lock_version+1 WHERE project_id=:p AND run_id=:r",
                p=project,
                r=run.run_id,
                m=next_manifest.manifest_id,
            )
            repo._bump(tx, project)

    def manifest(self, project, id):
        with self.repo.database.transaction() as tx:
            return InputManifest.model_validate(self._get(tx, project, "input_manifest", id))

    def save_result(self, project, record):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project, write=True)
            repo._lock(tx, project)
            run = TaskRun.model_validate(self._get(tx, project, "task_run", record.run_id))
            if run.task_id != record.payload.task_id:
                raise ContractError("result_task_mismatch")
            previous = one(
                tx,
                "SELECT payload FROM operational_records WHERE project_id=:p AND record_kind='task_result' AND parent_record_id=:r",
                p=project,
                r=record.run_id,
            )
            if previous and previous["payload"] != record.model_dump(mode="json"):
                raise Conflict("task_run_already_has_final_result")
            self._append(
                tx, project, "task_result", record.result_id, record.model_dump(mode="json"), record.run_id
            )
