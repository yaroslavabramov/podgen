"""One immutable script and source inventory shared by the two evaluation arms."""

from dataclasses import dataclass

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.source_reading import read_sources
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.reading_audit.evidence import current_catalog, snapshots, validate_retained
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.writing.research_materials import fact_source_mode
from podgen.writing.source_tools import SourceCorpus


@dataclass
class Case:
    command: WritingRunCommand
    manifest: dict
    script_ref: dict
    script: dict
    materials: object
    profile: object
    inventory: dict
    evidence: list
    snapshots: dict

    @property
    def binding(self):
        return dict(
            source_run=str(self.command.run_id),
            script_ref=self.script_ref,
            script_hash=digest(self.script),
            inventory_hash=digest(self.inventory),
            evidence_hash=digest(self.evidence),
            profile_hash=digest(self.profile.model_dump(mode="json")),
        )


def load(repo, generation, journal, source, stage):
    manifest = journal.manifest(source)
    command = WritingRunCommand.model_validate(manifest["command"])
    if command.actor_id != repo.actor_id or command.project_id != journal.project:
        raise Conflict("fact_scope_project_mismatch")
    invocation = journal.invocation(source, stage)
    if not invocation or invocation["status"] != "completed":
        raise Conflict("fact_scope_missing_script")
    current = journal.output(invocation)
    ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"])
    materials = WritingEvaluation(repo, lambda _: None)._materials(command)
    corpus = SourceCorpus(materials)
    inventory = {
        k: dict(title=v.title, sha256=v.digest, characters=len(v.text)) for k, v in corpus.documents.items()
    }
    offered = SourceCorpus(materials, fact_input_mode=fact_source_mode.VERSION)
    if {key: (doc.digest, len(doc.text)) for key, doc in offered.documents.items()} != {
        key: (row["sha256"], row["characters"]) for key, row in inventory.items()
    }:
        raise Conflict("fact_scope_incomplete_frozen_corpus")
    profile = load_profile(generation, journal.project, command.reviewer_profile_ref, "fact-review")
    with journal.read_operation(), read_sources((*materials.documents, *materials.runtime_admissions)):
        snapshot = {row["key"]: row for row in snapshots(materials)}
        # Retain passages, never the historical verdicts. Associate prior reads to a
        # section only through an exact anchor still present in the tested script.
        fact = journal.read(RuntimeRef.model_validate(current["reading_refs"]["fact"]))
        archive = fact["reading_audit"]["resolved_evidence"]
        rows = {key: set() for key in archive}
        for decision in fact["output"]["audit"]["claim_checks"]:
            indices = {
                i
                for anchor in decision["anchors"]
                for i, line in enumerate(current["output"]["utterances"])
                if anchor["quote"] in line["text"]
            }
            for evidence in decision["source_refs"]:
                rows[evidence["from_review"] + ":" + evidence["evidence_id"]].update(indices)
        admitted = {}

        def add(value, indices):
            validate_retained(value, materials, snapshot)
            ident = digest(value["source_selectors"])
            if ident not in admitted:
                admitted[ident] = dict(receipt=value, indices=set())
            admitted[ident]["indices"].update(indices)

        for key, indices in rows.items():
            add(archive[key], indices)
        # Evidence used in the interrupted final check is available equally to
        # both arms. Passages without a prior matching anchor stay searchable.
        final_stage = "fact-review-" + stage.split("-")[-1]
        last = journal.invocation(source, final_stage)
        if last is not None:
            for value in current_catalog(journal, last, materials, snapshot, corpus).values():
                add(value, ())
        evidence = [dict(receipt=x["receipt"], indices=sorted(x["indices"])) for x in admitted.values()]
    return Case(
        command,
        manifest,
        ref.model_dump(mode="json"),
        current["output"],
        materials,
        profile,
        inventory,
        evidence,
        snapshot,
    )


def relevant(case, start, stop):
    return [
        item["receipt"] for item in case.evidence if any(start <= index < stop for index in item["indices"])
    ]


def supplied_passages(case, proofs):
    """Bind supplied H passages to this authorized document snapshot.

    A different unit's private provider receipt is readable only in its own
    journal. This unit receives a fresh, byte-checked document excerpt. The
    experiment binding retains the digest of the originating receipt.
    """
    with read_sources((*case.materials.documents, *case.materials.runtime_admissions)):
        result = {}
        for alias, original in proofs.items():
            validate_retained(original, case.materials, case.snapshots)
            result[alias] = {
                key: original[key]
                for key in ("original_id", "kind", "content", "content_hash", "source_selectors", "locator")
                if key in original
            }
        return result
