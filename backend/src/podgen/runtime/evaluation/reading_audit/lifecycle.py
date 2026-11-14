"""Validate complete native audits and bind their compact memory and retained evidence."""

from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.source_reading import read_sources
from podgen.writing.model_references import context_references
from podgen.writing.reading_audit import VERSION
from podgen.writing.reading_audit.validation import validate
from podgen.writing.roles import WriteOutput
from podgen.writing.source_tools import SourceCorpus

from ..reading_workflow import base_stage
from ..recheck_request import semantic_request
from ..request_payload import request_payload
from .evidence import current_catalog, resolve, validate_retained
from .source import prepare


def _locator(value, materials, snapshot_index, corpus):
    validate_retained(value, materials, snapshot_index)
    if "locator" in value:
        if corpus.locate(value["locator"]) != value["content"]:
            raise ContractError("audit_source_read_binding_changed")
        return value["locator"]
    primary = [source.key for source in materials.packet.documents if source.purpose == "primary"]
    for selector in value["source_selectors"]:
        if selector["key"] in primary:
            source = f"S{primary.index(selector['key']) + 1}"
            locator = f"{source}:{selector['sha256']}:{selector['start']}:{selector['end']}"
            if corpus.locate(locator)["text"] != value["content"]["text"]:
                raise ContractError("audit_source_passage_mismatch")
            return locator
    raise ContractError("audit_finding_read_requires_primary_source")


def finalize(journal, invocation, parsed, audit, materials, script):
    documents = (*materials.documents, *getattr(materials, "runtime_admissions", ()))
    with journal.read_operation(), read_sources(documents):
        output, report = _finalize(journal, invocation, parsed, audit, materials, script)
    # Retained evidence is now an actual semantic input of the saved report.
    # Bind only the receipts/selectors authenticated above, once at finalization.
    journal.execution.prepare_context(invocation["run_id"], report["resolved_evidence"])
    return output, report


def _finalize(journal, invocation, parsed, audit, materials, script):
    fact = base_stage(invocation["stage"]) == "fact-review"
    parsed.validate_against(WriteOutput.model_validate(script))
    output = parsed.model_dump(mode="json")
    validate(output, script, audit.view, fact=fact, consistency_contract=audit.consistency_contract)
    retained = {}
    if fact:
        corpus = SourceCorpus(materials)
        current = current_catalog(journal, invocation, materials, audit.snapshot_index, corpus)
        if "packet_evidence_hash" in audit.binding:
            from .memory import fingerprint

            if fingerprint(current) != audit.binding["packet_evidence_hash"]:
                raise ContractError("audit_shared_packet_binding_mismatch")
        retained = resolve(output, current, audit.previous_evidence, materials, audit.snapshot_index)
        for finding in output["findings"]:
            aliases = finding["source_read_ids"]
            if len(aliases) != len(set(aliases)):
                raise ContractError("audit_duplicate_finding_source")
            locators = []
            for alias in aliases:
                old = alias.startswith("H")
                table = audit.previous_evidence if old else current
                if alias not in table:
                    raise ContractError("audit_finding_source_not_delivered")
                value = table[alias]
                locators.append(_locator(value, materials, audit.snapshot_index, corpus))
                retained[("previous:" if old else "current:") + alias] = deepcopy(value)
            finding["source_read_ids"] = locators
    return output, dict(contract=VERSION, binding=deepcopy(audit.binding), resolved_evidence=retained)


def verify_saved(
    journal, run_id, stage, manifest, materials, script, script_ref, invocation, context, record
):
    documents = (*materials.documents, *getattr(materials, "runtime_admissions", ()))
    with journal.read_operation(), read_sources(documents):
        return _verify_saved(
            journal, run_id, stage, manifest, materials, script, script_ref, invocation, context, record
        )


def _verify_saved(
    journal, run_id, stage, manifest, materials, script, script_ref, invocation, context, record
):
    audit = prepare(journal, run_id, stage, manifest, materials, script, script_ref)
    if audit is None or context.get("reading_audit") != audit.binding:
        raise Conflict("audit_saved_context_mismatch")
    body = semantic_request(journal, run_id, stage, invocation, context, script, record["output"])
    sent = request_payload(body).get("audit_context")
    references = context_references(context)
    if references:
        sent = references.translate(sent, inbound=True)
    sent = deepcopy(sent)
    if sent is not None:
        sent.pop("packet_evidence_index", None)
    if sent != audit.view or digest(sent) != audit.binding["memory_view_hash"]:
        raise Conflict("audit_saved_memory_mismatch")
    from podgen.writing.reading_audit.registry import model

    raw = record["wire_output"]
    parsed = model(stage).model_validate(references.translate(raw, inbound=True) if references else raw)
    output, proof = finalize(journal, invocation, parsed, audit, materials, script)
    if record["output"] != output or record.get("reading_audit") != proof:
        raise Conflict("audit_saved_report_mismatch")
