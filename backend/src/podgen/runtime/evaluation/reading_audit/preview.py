"""Freeze the same audit memory and source script used by ordinary stage execution."""

from dataclasses import replace

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.writing.reading_audit.registry import from_manifest

from ..reading_workflow import base_stage
from .source import prepare


def attach(snapshot, journal, run_id, stage, materials):
    if from_manifest(snapshot.manifest) is None or base_stage(stage) not in ("reader-review", "fact-review"):
        return snapshot
    base = base_stage(stage)
    number = int(stage.rsplit("-", 1)[1]) if stage != base else 0
    ref = snapshot.manifest["command"].get("initial_script_ref") if number == 0 else None
    source = journal.invocation(run_id, f"revise-{number}" if number else "draft")
    if source and source["status"] == "completed":
        ref = RuntimeRef(record_kind="tool_execution", record_id=source["output_record_id"]).model_dump(
            mode="json"
        )
    if ref is None:
        raise Conflict("preview_audit_native_script_missing")
    script = journal.read(RuntimeRef.model_validate(ref))["output"]
    context = prepare(journal, run_id, stage, snapshot.manifest, materials, script, ref)
    return replace(snapshot, script=script, reading_audit_context=context.view)
