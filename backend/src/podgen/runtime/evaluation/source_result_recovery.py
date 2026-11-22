"""Explicit local material-result recovery, stopping before external input counting."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.repository import Repository
from podgen.runtime.models.research_continuation import append_response
from podgen.runtime.models.research_turn_request import normal_request
from podgen.runtime.models.tool_result_publication import prepare_result
from podgen.writing.research_materials.fact_navigation import history_enabled as navigation_enabled

from .journal import EvaluationJournal
from .profiles import load_profile
from .source_result_evidence import FAILURE, VERSION, load
from .source_result_replay import existing
from .source_tool_contract import from_manifest


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def recover(repo, project, run_id, *, stage, invocation_id, call_id):
    """No provider/fetcher is accepted: only immutable local receipts can be used."""
    journal = EvaluationJournal(repo, project)
    with journal.lock(run_id):
        row = journal.run(run_id)
        if row is None or row["actor_id"] != repo.actor_id:
            raise AccessDenied()
        saved = existing(journal, run_id, stage, invocation_id, call_id)
        if saved is not None:
            _ready_projection(journal, run_id)
            return saved
        evidence = load(journal, run_id, stage, invocation_id, call_id)
        original = evidence.original
        request_record = journal.read(
            RuntimeRef(record_kind="tool_execution", record_id=evidence.root["request_record_id"])
        )
        generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
        profile = load_profile(
            generation, project, ArtifactRef.model_validate(request_record["context"]["profile_ref"]), stage
        )
        contract = from_manifest(journal.manifest(run_id))
        result = evidence.result
        if navigation_enabled(contract, stage):
            from podgen.runtime.models.fact_navigation import source_result

            result = source_result(result)
        conversation = deepcopy(evidence.request["input"])
        append_response(conversation, evidence.response, profile.provider)
        conversation.append(
            dict(type="function_call_output", call_id=call_id, output=encoded(result).decode())
        )
        next_turn = evidence.turn["turn_index"] + 1
        candidate = normal_request(
            original,
            conversation,
            evidence.root,
            next_turn,
            contract,
            profile.provider,
        )
        if profile.provider == "openai":
            from podgen.runtime.models.responses_wire import responses_request

            wire = responses_request(candidate)
        else:
            from podgen.runtime.models.messages_wire import messages_request

            wire = messages_request(candidate)
        # The material cache is a generic immutable owner. Its entire closure,
        # including unavailable acquisitions, must be admitted before publication.
        journal.execution.prepare(run_id)
        journal.execution.prepare(run_id, evaluation=[evidence.request_ref, evidence.result_ref])
        phase, result_ref = prepare_result(journal, evidence.root, evidence.call, evidence.result)
        request_ref = phase.record(
            dict(
                contract=VERSION,
                stage=stage,
                next_turn=next_turn,
                request_blob=phase.blob(encoded(candidate)).model_dump(mode="json"),
                wire_request_blob=phase.blob(encoded(wire)).model_dump(mode="json"),
                request_hash=digest(candidate),
                wire_request_hash=digest(wire),
                material_request_ref=evidence.request_ref.model_dump(mode="json"),
                material_result_ref=evidence.result_ref.model_dump(mode="json"),
                tool_result_ref=result_ref.model_dump(mode="json"),
            )
        )
        outcome = dict(
            contract=VERSION,
            run_id=str(run_id),
            invocation_id=str(invocation_id),
            call_id=call_id,
            next_turn=next_turn,
            status="prepared",
            stop_reason="before_external_count",
            request_hash=digest(candidate),
            next_request_ref=request_ref.model_dump(mode="json"),
            tool_result_ref=result_ref.model_dump(mode="json"),
        )
        phase.event(
            "source_material_result_recovered",
            data=dict(
                policy=VERSION,
                turn_index=evidence.turn["turn_index"],
                call_id=call_id,
                previous_failure=FAILURE,
                outcome=outcome,
            ),
        )
        # The existing tool-result checkpoint checks the exact paid provider-call
        # proof. Its atomic state is response_completed; no replacement is created.
        phase.transition("tool_running")
        phase.commit()
        _ready_projection(journal, run_id)
        return outcome


def _ready_projection(journal, run_id):
    row = journal.run(run_id)
    if row["status"] == "needs_attention" and row["failure_code"] == FAILURE:
        # Repair this disposable projection after the durable checkpoint. A crash
        # here is repaired by the same idempotent local operation on the next call.
        journal.status(run_id, "prepared")
