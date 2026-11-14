"""Lossless private/3 content identity and archive row encoding, without SQL."""

import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from podgen.domain.errors import ContractError

from .contracts import PrivateObjectUse

JSON_COLUMNS = (
    "data",
    "archive_views",
    "archive_payloads",
    "charge_changes",
    "owned_objects",
    "object_origins",
)


def _json(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(child) for child in value]
    return value


def content(command):
    return dict(
        kind=command.kind,
        expected_revision=command.expected_revision,
        operation_id=str(command.operation_id),
        attempt_id=str(command.attempt_id),
        step_id=str(command.step_id) if command.step_id else None,
        step_index=command.step_index,
        input_set_id=str(command.input_set_id),
        observation_no=command.observation_no,
        data=_json(command.data),
        views=_json(command.views),
        objects={slot: obj.reference.model_dump(mode="json") for slot, obj in command.objects.items()},
        object_origins={
            slot: str(obj.owner_entry_id)
            for slot, obj in command.objects.items()
            if isinstance(obj, PrivateObjectUse)
        },
        parents=[str(parent) for parent in command.parents],
        head=_json(asdict(command.head)),
        charges=_json([asdict(charge) for charge in command.charges]),
        semantic_result=dict(
            reference=command.semantic_result.reference.model_dump(mode="json"),
            payload=command.semantic_result.payload,
            source_entries=[str(entry) for entry in command.semantic_result.source_entries],
        )
        if command.semantic_result
        else None,
    )


def encode(command):
    body = content(command)
    fingerprint = hashlib.sha256(
        json.dumps(body, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()
    return body, fingerprint


def json_value(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def entry_row(project, command, head, fingerprint, body, charge_facts):
    full_views = body["views"]
    views = [{key: value for key, value in view.items() if key != "payload"} for view in full_views]
    for view, full in zip(views, full_views, strict=True):
        payload = full["payload"]
        if "event_kind" in payload:
            view["event"] = dict(
                kind=payload["event_kind"], source_turn_index=payload["data"].get("source_turn_index")
            )
    aliases = [UUID(view["record_id"]) for view in views]
    if len(aliases) != len(set(aliases)):
        raise ContractError("private_duplicate_archive_reference")
    return dict(
        project_id=project,
        run_id=command.run_id,
        entry_id=command.entry_id,
        revision=head["revision"],
        commit_key=command.commit_key,
        content_hash=fingerprint,
        checkpoint_kind=command.kind,
        operation_id=command.operation_id,
        attempt_id=command.attempt_id,
        step_id=command.step_id,
        step_index=command.step_index,
        observation_no=command.observation_no,
        input_set_id=command.input_set_id,
        projection_version=3,
        data=json_value(body["data"]),
        archive_views=json_value(views),
        archive_payloads=json_value([view["payload"] for view in full_views]),
        charge_changes=json_value(charge_facts),
        charge_operations=[charge.operation_id for charge in command.charges],
        archive_ids=aliases,
        owned_objects=json_value(body["objects"]),
        object_origins=json_value(body["object_origins"]),
        owned_hashes=sorted({obj.reference.sha256 for obj in command.objects.values()}),
        parent_entries=list(command.parents),
    )
