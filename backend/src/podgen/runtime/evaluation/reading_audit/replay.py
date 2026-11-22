"""Outcome replay uses the same complete audit validation as the original stage."""

from ..contracts import WritingRunCommand
from .lifecycle import verify_saved


def load_materials(journal, manifest):
    """The run's own material loader, including sources admitted while reading."""
    from ..service import WritingEvaluation

    command = WritingRunCommand.model_validate(manifest["command"])
    return WritingEvaluation(journal.repo, None)._materials(command)


def verify_outcome_reading(
    journal, run_id, stage, manifest, script, script_ref, invocation, context, record, materials
):
    verify_saved(journal, run_id, stage, manifest, materials, script, script_ref, invocation, context, record)
