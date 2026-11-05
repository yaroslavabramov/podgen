from uuid import UUID, uuid4

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.database import json_text, one, query


class EditorialBranches:
    def __init__(self, repository):
        self.repo = repository

    def bootstrap(self, project_id, episode_id, spec_ref, *, producer, operation_id, branch_id):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, write=True)
            repo._lock(tx, project_id)
            spec = repo._resolve(tx, project_id, spec_ref)
            if spec_ref.kind != "EpisodeSpec" or spec["episode_id"] != episode_id:
                raise ContractError("bootstrap_spec_episode_mismatch")
            payload = dict(
                episode_spec_ref=spec_ref.model_dump(mode="json"),
                dossier_ref=None,
                main_question=spec["payload"]["main_question"] or spec["payload"]["topic"],
                working_answer=None,
                answer_basis_refs=[],
                alternatives=[],
                listener_promise=None,
                active_question_refs=[],
                story_plan_ref=None,
                script_ref=None,
                current_decision_refs=[],
                phase="orienting",
            )
            state = repo._create(
                tx,
                project_id,
                "EditorialState",
                payload,
                producer=producer,
                operation_id=operation_id,
                episode_id=episode_id,
            )
            previous = one(
                tx,
                "SELECT * FROM branch_heads WHERE project_id=:p AND episode_id=:e AND branch_id=:b",
                p=project_id,
                e=episode_id,
                b=branch_id,
            )
            if previous:
                if previous["editorial_state_id"] != state.ref.revision_id:
                    raise Conflict("branch_already_initialized")
                return state
            query(
                tx,
                "INSERT INTO branch_heads VALUES(:p,:e,:b,:state,0,0)",
                p=project_id,
                e=episode_id,
                b=branch_id,
                state=state.ref.revision_id,
            )
            return state

    def integrate(
        self,
        project_id,
        episode_id,
        branch_id,
        expected_head,
        expected_epoch,
        decision_ref,
        state_payload,
        *,
        producer,
        operation_id,
    ):
        repo = self.repo
        with repo.database.transaction() as tx:
            role = repo._acl(tx, project_id, write=True)
            if role not in ("owner", "editor", "evaluator"):
                raise AccessDenied()
            repo._lock(tx, project_id)
            head = one(
                tx,
                """SELECT * FROM branch_heads WHERE project_id=:p AND episode_id=:e AND branch_id=:b FOR UPDATE""",
                p=project_id,
                e=episode_id,
                b=branch_id,
            )
            fingerprint = digest(
                dict(
                    actor_id=str(repo.actor_id),
                    episode_id=str(episode_id),
                    branch_id=str(branch_id),
                    expected_head=expected_head.model_dump(mode="json"),
                    expected_epoch=expected_epoch,
                    decision_ref=decision_ref.model_dump(mode="json"),
                    state_payload=state_payload,
                    producer=producer.model_dump(mode="json")
                    if hasattr(producer, "model_dump")
                    else producer,
                )
            )
            receipt = one(
                tx,
                "SELECT * FROM mutation_receipts WHERE project_id=:p AND operation_kind='editorial_integrate' AND operation_id=:op",
                p=project_id,
                op=operation_id,
            )
            if receipt and (
                receipt["actor_id"] != repo.actor_id or receipt["input_fingerprint"] != fingerprint
            ):
                raise Conflict("operation_id_reused_with_different_input")
            if head is None or head["generation_epoch"] != expected_epoch:
                raise Conflict("branch_head_or_epoch_conflict")
            # Validate current access even when returning a committed historical result.
            repo._resolve(tx, project_id, expected_head)
            repo._resolve(tx, project_id, decision_ref)
            if receipt:
                result = repo._resolve(
                    tx, project_id, ArtifactRef.model_validate(receipt["output"]["state_ref"])
                )
                return repo._envelope(result), UUID(receipt["output"]["event_id"])
            if head["editorial_state_id"] != expected_head.revision_id:
                raise Conflict("branch_head_or_epoch_conflict")
            decision = repo._resolve(tx, project_id, decision_ref)
            if (
                decision_ref.kind != "EditorialDecision"
                or decision["payload"]["state_ref"] != expected_head.model_dump(mode="json")
                or decision_ref.model_dump(mode="json") not in state_payload["current_decision_refs"]
            ):
                raise ContractError("integration_decision_binding_mismatch")
            previous = repo._resolve(tx, project_id, expected_head)
            if state_payload["episode_spec_ref"] != previous["payload"]["episode_spec_ref"]:
                raise ContractError("integration_changes_spec")
            state = repo._create(
                tx,
                project_id,
                "EditorialState",
                state_payload,
                producer=producer,
                operation_id=operation_id,
                episode_id=episode_id,
                artifact_id=expected_head.artifact_id,
            )
            event_id = uuid4()
            body = dict(
                event_id=str(event_id),
                expected_head=expected_head.model_dump(mode="json"),
                next_head=state.ref.model_dump(mode="json"),
                policy_ref=repo._resolve(
                    tx, project_id, ArtifactRef.model_validate(state_payload["episode_spec_ref"])
                )["payload"]["policy_ref"],
                reviewed_output_refs=decision["payload"]["reviewed_output_refs"],
                generation_epoch=expected_epoch,
                branch_id=str(branch_id),
                episode_id=str(episode_id),
            )
            query(
                tx,
                "INSERT INTO integration_events VALUES(:p,:id,:decision,CAST(:body AS json))",
                p=project_id,
                id=event_id,
                decision=decision_ref.revision_id,
                body=json_text(body),
            )
            query(
                tx,
                """UPDATE branch_heads SET editorial_state_id=:state,lock_version=lock_version+1
                         WHERE project_id=:p AND episode_id=:e AND branch_id=:b""",
                p=project_id,
                e=episode_id,
                b=branch_id,
                state=state.ref.revision_id,
            )
            query(
                tx,
                "INSERT INTO outbox(event_id,project_id,event_kind,payload) VALUES(:id,:p,'editorial_integration',CAST(:body AS json))",
                p=project_id,
                id=event_id,
                body=json_text(body),
            )
            query(
                tx,
                "INSERT INTO mutation_receipts VALUES(:p,'editorial_integrate',:op,:actor,:fp,CAST(:body AS json))",
                p=project_id,
                op=operation_id,
                actor=repo.actor_id,
                fp=fingerprint,
                body=json_text(dict(state_ref=state.ref.model_dump(mode="json"), event_id=str(event_id))),
            )
            repo._bump(tx, project_id)
            return state, event_id
