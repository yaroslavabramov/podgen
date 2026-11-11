"""Pin inherited resources and explicit added allowances without upgrading the source run."""

from copy import deepcopy

from podgen.domain.errors import ContractError
from podgen.persistence.repository import timestamp

from ..contracts import WritingRunCommand, command_payload
from .source import KEY, STAGES, source_binding


def assemble(command, parent, binding):
    source = WritingRunCommand.model_validate(parent["command"])
    last = source.max_script_revisions
    mapping = {
        "revise-1": f"revise-{last}",
        "reader-review-1": f"reader-review-{last}",
        "fact-review-1": f"fact-review-{last}",
    }
    manifest = deepcopy(parent)
    manifest.update(
        command=command_payload(command), stages=list(STAGES), created_at=timestamp(), **{KEY: binding}
    )
    manifest.pop("planning_continuation", None)
    manifest.pop("initial_script_hash", None)
    for field in ("prompt_refs", "role_instructions_versions", "response_schemas"):
        manifest[field] = {stage: deepcopy(parent[field][original]) for stage, original in mapping.items()}
    if "writer_response_contract" in parent:
        from podgen.writing.author_reply import contract_pin, verify_pin

        labels = parent["writer_response_contract"]["speakers"]
        verify_pin(parent, labels)
        manifest["writer_response_contract"] = contract_pin(manifest, labels)
    return manifest


def create(command, journal, materials):
    binding, parent, _ = source_binding(journal, command, materials)
    return journal.create_run(command, assemble(command, parent, binding))


def verify_assembly(command, parent, binding, actual):
    expected = assemble(command, parent, binding)
    if {k: v for k, v in actual.items() if k != "created_at"} != {
        k: v for k, v in expected.items() if k != "created_at"
    }:
        raise ContractError("editorial_continuation_resources_changed")
