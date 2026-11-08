"""A writer's material request is a durable terminal result, never a partial script."""

import json
from uuid import UUID, uuid5

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import one
from podgen.writing.author_reply import MaterialRequest, offered

from .usage import attempt_usage
from .validation import RoleOutputError, load_unique_json

CONTRACT = "writer-material-request/1"


class NeedsWritingMaterial(Exception):
    def __init__(self, result):
        self.result = result


def unwrap(raw, stage, projection):
    if not offered(stage, projection):
        return raw
    try:
        data = load_unique_json(raw)
    except ValueError as error:
        raise RoleOutputError([{"code": "invalid_author_result_json"}]) from error
    if not isinstance(data, dict) or set(data) != {"result"} or not isinstance(data["result"], dict):
        raise RoleOutputError([{"code": "expected_author_result"}])
    work = data["result"]
    if "material_request" in work:
        try:
            return MaterialRequest.model_validate(work)
        except ValidationError as error:
            raise RoleOutputError(
                [dict(path=list(e["loc"]), type=e["type"], message=e["msg"]) for e in error.errors()]
            ) from error
    try:
        return json.dumps(work, ensure_ascii=False, allow_nan=False)
    except ValueError as error:
        raise RoleOutputError([{"code": "invalid_author_result_json"}]) from error


def outcome_ref(run_id):
    return RuntimeRef(record_kind="tool_execution", record_id=uuid5(run_id, CONTRACT))


def result(record):
    return {
        "run_id": record["run_id"],
        "case_id": record["case_id"],
        "status": "needs_attention",
        "review_status": "needs_material",
        "failure_code": "writer_needs_material",
        "stage": record["stage"],
        "material_request": record["material_request"],
        "material_request_ref": outcome_ref(UUID(record["run_id"])).model_dump(mode="json"),
        "last_script_ref": record["last_script_ref"],
        "token_usage_or_reservation": record["token_usage_or_reservation"],
        "semantic_acceptance": False,
        "production_ready": False,
    }


def pause(journal, command, invocation, query, *, script_ref, script, totals, prior, profile):
    current = journal.invocation(command.run_id, invocation["stage"])
    usage = attempt_usage(journal, current, profile.output_limit)
    ref = outcome_ref(command.run_id)
    record = {
        "contract": CONTRACT,
        "run_id": str(command.run_id),
        "case_id": command.case_id,
        "stage": current["stage"],
        "invocation_id": str(current["invocation_id"]),
        "request_ref": RuntimeRef(
            record_kind="tool_execution", record_id=current["request_record_id"]
        ).model_dump(mode="json"),
        "material_request": query.material_request.model_dump(mode="json"),
        "last_script_ref": script_ref,
        "last_script_hash": digest(script) if script is not None else None,
        "token_usage_or_reservation": {k: totals[k] + prior[k] + usage[k] for k in usage},
    }
    journal.record(record, record_id=ref.record_id)
    journal.transition(current, "completed", output_ref=ref)
    raise NeedsWritingMaterial(result(record))


def saved_result(journal, command):
    """The invocation transition commits the outcome, even if the process then stops."""
    ref = outcome_ref(command.run_id)
    with journal.repo.database.read_transaction() as tx:
        journal.authorize(tx)
        saved = one(
            tx,
            """SELECT invocation_id,stage,request_record_id FROM evaluation_model_invocations
               WHERE project_id=:p AND run_id=:r AND output_record_id=:ref AND status='completed'""",
            p=command.project_id,
            r=command.run_id,
            ref=ref.record_id,
        )
    if saved is None:
        return None
    record = journal.read(ref)
    if (
        record.get("contract") != CONTRACT
        or record["run_id"] != str(command.run_id)
        or record["stage"] != saved["stage"]
        or record["invocation_id"] != str(saved["invocation_id"])
        or record["request_ref"]["record_id"] != str(saved["request_record_id"])
    ):
        raise Conflict("writer_material_request_binding_mismatch")
    script_ref = record["last_script_ref"]
    if (
        script_ref
        and digest(journal.read(RuntimeRef.model_validate(script_ref))["output"])
        != record["last_script_hash"]
    ):
        raise Conflict("writer_material_script_binding_mismatch")
    return result(record)
