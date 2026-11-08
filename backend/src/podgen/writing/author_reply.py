"""One wire-only author choice: deliver the requested work or ask for a necessary source."""

from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict

from .projection_versions import V9
from .revision.contracts import OpenResearchQuestion
from .roles import RoleOutput


class MaterialRequest(RoleOutput):
    material_request: OpenResearchQuestion


def offered(stage, projection):
    return projection == V9 and stage in {"draft", "revise", "revise-1", "revise-2"}


def wire_schema(work, *, speaker_labels=()):
    """Keep references at document root and keep the public script/patch unchanged."""
    work = deepcopy(work)
    if speaker_labels:
        from .speaker_schema import bind_script_speakers

        work = bind_script_speakers(work, speaker_labels)
    request = MaterialRequest.model_json_schema()
    definitions = work.pop("$defs", {})
    query = request.pop("$defs")["OpenResearchQuestion"]
    # Structural rewrites can already reference this exact published question type.
    if "OpenResearchQuestion" in definitions and definitions["OpenResearchQuestion"] != query:
        raise ValueError("author_reply_definition_collision")
    definitions["OpenResearchQuestion"] = query
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {
                "description": (
                    "Return the requested work. If essential missing source material prevents this "
                    "draft or revision, return one specific material_request explaining where it is "
                    "needed and why the supplied evidence cannot support the passage."
                ),
                "anyOf": [work, request],
            }
        },
        "required": ["result"],
        "$defs": definitions,
    }


def contract_pin(manifest, speaker_labels):
    """Pin the real forms, including the base of each dynamically addressed edit mode."""
    stages = {
        stage: digest(wire_schema(schema, speaker_labels=speaker_labels))
        for stage, schema in manifest["response_schemas"].items()
        if offered(stage, manifest["role_projection_version"])
    }
    modes = {
        mode: digest(wire_schema(schema, speaker_labels=speaker_labels))
        for mode, schema in manifest.get("revision_response_schemas", {}).items()
    }
    return {
        "version": "author-result/1",
        "speakers": list(speaker_labels),
        "stages": stages,
        "revision_modes": modes,
    }


def verify_pin(manifest, speaker_labels):
    if manifest.get("role_projection_version") == V9 and manifest.get(
        "writer_response_contract"
    ) != contract_pin(manifest, speaker_labels):
        raise Conflict("author_reply_contract_changed")
