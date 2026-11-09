"""Bind private revision outputs to the exact stored script and both stored reviews."""

from copy import deepcopy
from dataclasses import dataclass

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.writing.revision.context import from_manifest as revision_context_from_manifest
from podgen.writing.story_views import editorial_frame

from . import script_revision


def source_state(journal, ref, script):
    record = journal.read(RuntimeRef.model_validate(ref))
    visited = set()
    while True:
        if record.get("output") != script:
            raise Conflict("revision_source_version_mismatch")
        if "composition" in record:
            return record["composition"]
        if record.get("contract") != "independent-reading-outcome/1":
            return None
        target = RuntimeRef.model_validate(record["script_ref"])
        if target.record_id in visited:
            raise Conflict("revision_source_cycle")
        visited.add(target.record_id)
        record = journal.read(target)


@dataclass(frozen=True)
class RevisionInvocation:
    selection: dict
    schema: dict
    script: dict
    composition: dict | None
    speakers: tuple
    unresolved_questions: list

    @property
    def model(self):
        return script_revision.output_model(self.selection)

    @property
    def context(self):
        return dict(
            revision_contract=script_revision.VERSION,
            revision_operation_hash=digest(self.selection),
            revision_operation=deepcopy(self.selection),
            revision_schema_hash=digest(self.schema),
            composition_hash=digest(self.composition),
            unresolved_questions=deepcopy(self.unresolved_questions),
        )

    @property
    def inputs(self):
        return dict(
            revision_operation=script_revision.model_operation(self.selection), composition=self.composition
        )

    def apply(self, wire):
        return script_revision.apply(
            self.selection,
            wire,
            script=self.script,
            composition=self.composition,
            speakers=self.speakers,
            expected_hash=digest(self.script),
            unresolved_questions=self.unresolved_questions,
        )

    def verify(self, record):
        output, state, proof = self.apply(record["revision"]["wire_output"])
        if record["output"] != output or record["composition"] != state or record["revision"] != proof:
            raise Conflict("revision_application_mismatch")


def prepare(journal, stage, manifest, *, script, script_ref, readings, reading_refs, speakers, approved_plan):
    if not stage.startswith("revise-") or not script_revision.enabled(manifest):
        return None
    composition = source_state(journal, script_ref, script)
    questions = (composition or {}).get("unresolved_questions")
    if questions is None:
        questions = (
            editorial_frame(approved_plan, projection_version=manifest["role_projection_version"])[
                "unresolved_questions"
            ]
            if approved_plan
            else []
        )
    if set(readings or {}) != {"reader", "fact"} or set(reading_refs or {}) != {"reader", "fact"}:
        raise Conflict("revision_readings_missing")
    for origin, ref in reading_refs.items():
        record = journal.read(RuntimeRef.model_validate(ref))
        if (
            record["output"] != readings[origin]
            or record["script_ref"] != script_ref
            or record["script_hash"] != digest(script)
        ):
            raise Conflict("revision_readings_binding_mismatch")
    selection = script_revision.operation(
        script,
        readings,
        int(stage.rsplit("-", 1)[1]) - 1,
        revision_context_contract=revision_context_from_manifest(manifest),
    )
    return RevisionInvocation(
        selection,
        script_revision.bound_schema(manifest, selection),
        script,
        composition,
        tuple(speakers),
        questions,
    )
