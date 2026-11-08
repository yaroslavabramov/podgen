"""A descendant inherits frozen resources; paid source invocations are never copied or rerun."""

from copy import deepcopy

from podgen.persistence.repository import timestamp

from ..contracts import command_payload
from ..workflow import stages_for
from .source import KEY, source_binding


def assemble(command, parent, binding):
    manifest = deepcopy(parent)
    stages = stages_for(command)
    manifest.update(
        command=command_payload(command),
        stages=list(stages),
        created_at=timestamp(),
        **{KEY: binding},
    )
    for field in ("prompt_refs", "role_instructions_versions", "response_schemas"):
        manifest[field] = {stage: manifest[field][stage] for stage in stages}
    if "writer_response_contract" in manifest:
        from podgen.writing.author_reply import contract_pin, verify_pin

        labels = parent["writer_response_contract"]["speakers"]
        verify_pin(parent, labels)
        manifest["writer_response_contract"] = contract_pin(manifest, labels)
    return manifest


def create(command, journal, materials):
    binding, parent, _, _ = source_binding(journal, command, materials)
    return journal.create_run(command, assemble(command, parent, binding))
