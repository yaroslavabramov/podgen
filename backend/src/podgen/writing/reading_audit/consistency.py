"""Versioned report consistency; reviewers still choose materiality and finding priority."""

from podgen.domain.errors import ContractError

from . import KEY as AUDIT_KEY
from . import VERSION as AUDIT_VERSION

KEY = "reading_audit_consistency_contract"
VERSION = "decision-findings/1"


def from_manifest(manifest):
    version = manifest.get(KEY)
    if version is not None and (version != VERSION or manifest.get(AUDIT_KEY) != AUDIT_VERSION):
        raise ContractError("unsupported_reading_audit_consistency")
    return version


def validate(output, version):
    if version is None:
        return
    if version != VERSION:
        raise ContractError("unsupported_reading_audit_consistency")
    if "audit" not in output or "finding_links" not in output:
        raise ContractError("audit_consistency_report_missing")
    audit = output["audit"]
    decisions = audit.get("claim_checks", audit.get("judgments", []))
    ids = {row["decision_id"] for row in decisions}
    links = output["finding_links"]
    if sorted(row["finding_index"] for row in links) != list(range(len(output["findings"]))):
        raise ContractError("audit_findings_unlinked")
    linked = {key for row in links for key in row["decision_ids"]}
    if not linked <= ids:
        raise ContractError("audit_unknown_decision")
    negative = {
        row["decision_id"]
        for row in decisions
        if row["verdict"] in ("needs_change", "contradicted", "unsupported")
    }
    if not negative <= linked:
        raise ContractError("audit_negative_decision_unlinked")


def describe(schema, version):
    if version is None:
        return
    if version != VERSION:
        raise ContractError("unsupported_reading_audit_consistency")
    schema["properties"]["finding_links"]["description"] += (
        " Every current reader needs_change or fact contradicted or unsupported decision must be linked to an actual finding."
        " Choose priority and recommendation using the existing rules of your role; optional may accompany keep;"
        " important or blocking findings require revision. An unchecked decision"
        " does not automatically require a finding or a mandatory repair."
    )
