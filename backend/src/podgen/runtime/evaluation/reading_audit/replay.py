"""Outcome replay uses the same complete audit validation as the original stage."""

from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials

from ..contracts import WritingRunCommand
from .lifecycle import verify_saved


def load_materials(journal, manifest):
    command = WritingRunCommand.model_validate(manifest["command"])
    return load_editorial_materials(
        generation_repo(journal.repo),
        journal.project,
        command.author_ref,
        case_id=command.case_id,
        fixed_ref=command.fixed_plan_input_ref,
    )


def verify_outcome_reading(
    journal, run_id, stage, manifest, script, script_ref, invocation, context, record, materials
):
    verify_saved(journal, run_id, stage, manifest, materials, script, script_ref, invocation, context, record)
