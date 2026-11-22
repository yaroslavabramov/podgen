"""Bind each memory view to its native prior report, writer assignment and exact script versions."""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.writing.projections import indexed_draft
from podgen.writing.reading_audit import VERSION
from podgen.writing.reading_audit.consistency import DELTA_VERSION
from podgen.writing.reading_audit.consistency import from_manifest as consistency_from_manifest
from podgen.writing.reading_audit.input import initial_context
from podgen.writing.reading_audit.memory_projection import STABLE_REFERENCES
from podgen.writing.reading_audit.memory_projection import from_manifest as memory_from_manifest
from podgen.writing.reading_audit.registry import from_manifest
from podgen.writing.reading_audit.validation import decisions
from podgen.writing.revision.task_verification import comparison

from ..reading_workflow import base_stage
from ..request_payload import request_payload
from .evidence import snapshots
from .memory import current_packet, fingerprint, project
from .reference_text import alias_number, fields, text_aliases


@dataclass(frozen=True)
class AuditContext:
    view: dict
    binding: dict
    previous_evidence: dict
    snapshot_index: dict
    consistency_contract: str | None = None
    retained_decisions: tuple[dict, ...] = ()


def prepare(journal, run_id, stage, manifest, materials, script, script_ref):
    if from_manifest(manifest) is None or base_stage(stage) not in ("reader-review", "fact-review"):
        return None
    consistency = consistency_from_manifest(manifest)
    memory_projection = memory_from_manifest(manifest)
    fact = base_stage(stage) == "fact-review"
    binding = dict(
        contract=VERSION,
        manifest_hash=digest(manifest),
        script_ref=script_ref,
        script_hash=digest(script),
        materials_hash=manifest["materials_hash"],
    )
    if consistency is not None:
        binding["consistency_contract"] = consistency
    snapshot_index = {row["key"]: row for row in snapshots(materials)} if fact else {}
    if fact:
        binding["source_snapshots"] = [
            {k: v for k, v in row.items() if k != "text"} for row in snapshot_index.values()
        ]
    packet = {}
    if memory_projection is not None:
        binding["memory_projection_contract"] = memory_projection
        if fact:
            packet = current_packet(materials, manifest, snapshot_index)
            binding["packet_evidence_hash"] = fingerprint(packet)
    view = initial_context(fact=fact)
    previous_evidence = {}
    retained_decisions = []
    stable_references = fact and memory_projection == STABLE_REFERENCES
    if stable_references:
        binding["evidence_reference_max"] = 0
    if stage != base_stage(stage):
        number = int(stage.rsplit("-", 1)[1])
        revision = journal.invocation(run_id, f"revise-{number}")
        if revision is None or revision["status"] != "completed":
            raise Conflict("audit_native_revision_missing")
        record = journal.output(revision)
        current_ref = RuntimeRef(
            record_kind="tool_execution", record_id=revision["output_record_id"]
        ).model_dump(mode="json")
        before = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
        from ..source_tool_trace import commission_invocation

        writer = request_payload(journal.request(commission_invocation(journal, revision)))
        if (
            current_ref != script_ref
            or record["output"] != script
            or record["script_hash"] != digest(before)
            or writer["script"] != indexed_draft(before)
        ):
            raise Conflict("audit_revision_binding_mismatch")
        role = "fact" if fact else "reader"
        prior_ref = RuntimeRef.model_validate(record["reading_refs"][role])
        prior = journal.read(prior_ref)
        prior_run = UUID(prior["run_id"])
        prior_manifest = journal.manifest(prior_run)
        prior_invocation = journal.invocation(prior_run, prior["stage"])
        if (
            base_stage(prior["stage"]) != base_stage(stage)
            or prior_invocation is None
            or prior_invocation["status"] != "completed"
            or prior_invocation["output_record_id"] != prior_ref.record_id
            or prior["script_ref"] != record["script_ref"]
            or prior["script_hash"] != digest(before)
            or from_manifest(prior_manifest) != VERSION
            or "reading_audit" not in prior
            or "audit" not in prior["output"]
        ):
            raise Conflict("audit_previous_report_missing_or_mismatched")
        if prior["reading_audit"]["binding"]["materials_hash"] != manifest["materials_hash"]:
            raise Conflict("audit_previous_materials_changed")
        bound = prior["reading_audit"]["binding"]
        if (
            bound["manifest_hash"] != digest(prior_manifest)
            or bound["script_ref"] != record["script_ref"]
            or bound["script_hash"] != digest(before)
        ):
            raise Conflict("audit_previous_context_changed")
        aliases = {}
        if fact:
            if stable_references:
                binding["evidence_reference_max"] = bound.get("evidence_reference_max", 0)
            aliases, previous_evidence, reused = project(
                prior["reading_audit"]["resolved_evidence"],
                packet,
                materials,
                snapshot_index,
                shared=memory_projection is not None,
                previous_max=binding.get("evidence_reference_max"),
            )
            if stable_references:
                text_references = text_aliases(prior["reading_audit"]["resolved_evidence"], aliases)
                binding["evidence_reference_max"] = max(
                    [binding["evidence_reference_max"], *(alias_number(key) for key in previous_evidence)]
                )
            if memory_projection is not None:
                binding["reused_packet_evidence"] = reused
        prior_decisions = []
        for native in decisions(prior["output"]):
            row = fields(native, text_references) if stable_references else dict(native)
            if fact:
                row["source_refs"] = [
                    dict(aliases[f"{ref['from_review']}:{ref['evidence_id']}"])
                    for ref in native["source_refs"]
                ]
            compact = {
                key: deepcopy(value) for key, value in row.items() if key not in ("rationale", "source_refs")
            }
            if fact:
                compact["source_refs"] = [dict(ref) for ref in row["source_refs"]]
            prior_decisions.append(compact)
            if fact and consistency == DELTA_VERSION:
                retained_decisions.append(row)
        compared = comparison(before, script, writer, prior["output"])
        assignments = compared["assignments"]
        if fact:
            assignments = [row for row in assignments if row["origin"] == "fact"]
        view.update(
            phase="recheck",
            assignments=assignments,
            previous_decisions=prior_decisions,
            previous_coverage=deepcopy(prior["output"]["audit"]["coverage"]),
            previous_recommendation=prior["output"]["recommendation"],
            previous_overall_judgment=prior["output"]["audit"]["overall_judgment"],
            previous_script_in_order=compared["previous_script_in_order"],
            revision_operation=compared["revision_operation"],
        )
        if fact and consistency == DELTA_VERSION:
            from podgen.writing.reading_audit.recheck_changes import attach

            attach(view, before, script, stable_occurrences=stable_references)
            complete_decisions = {row["decision_id"]: row for row in retained_decisions}
            for row in view["previous_decisions"]:
                if row["requires_update"]:
                    row["rationale"] = complete_decisions[row["decision_id"]]["rationale"]
        binding.update(
            prior_review_ref=prior_ref.model_dump(mode="json"),
            prior_report_hash=digest(prior),
            prior_manifest_hash=digest(prior_manifest),
            previous_script_ref=record["script_ref"],
            previous_script_hash=digest(before),
            revision_ref=current_ref,
            revision_hash=digest(record),
        )
    if fact:
        view["previous_evidence"] = [
            dict(
                evidence_id=alias,
                original_id=value["original_id"],
                kind=value["kind"],
                content=deepcopy(value["content"]),
            )
            for alias, value in previous_evidence.items()
        ]
        if consistency == DELTA_VERSION and view["phase"] == "recheck":
            from .. import source_tool_contract
            from .recheck_evidence import compact

            tools = source_tool_contract.from_manifest(manifest)
            readable = bool(
                tools
                and source_tool_contract.applies(stage, tools)
                and any(tool.get("name") == "read_source" for tool in tools.get("definitions", ()))
            )
            compact(
                view,
                previous_evidence,
                materials,
                snapshot_index,
                read_source_available=readable,
            )
    binding["memory_view_hash"] = digest(view)
    binding["previous_evidence_hash"] = digest(previous_evidence)
    return AuditContext(
        view, binding, previous_evidence, snapshot_index, consistency, tuple(retained_decisions)
    )
