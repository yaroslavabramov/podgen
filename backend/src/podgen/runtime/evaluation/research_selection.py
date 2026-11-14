"""Immutable supplemental selections owned by one accepted planning clearance.

The journal is examined once at acceptance (or first use of a legacy clearance).
Later readers authorize the saved selection and its exact clearance, not the whole
planning conversation. Empty selections are explicit results too.
"""

from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import one
from podgen.persistence.read_authorization import _read_connection

from .source_tool_recovery import lineage

VERSION = "accepted-plan-research-selection/1"


def selection_ref(run_id):
    return RuntimeRef(record_kind="tool_execution", record_id=uuid5(run_id, VERSION))


def _read_optional(journal, ref):
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        exists = one(
            tx,
            "SELECT 1 FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=journal.project,
            r=ref.record_id,
        )
    return journal.read(ref) if exists else None


def load(journal, run_id):
    from .planning import clearance_ref

    with journal.read_operation():
        clearance = _read_optional(journal, clearance_ref(run_id))
        if clearance is None:
            return {}
        saved = _read_optional(journal, selection_ref(run_id))
    if saved is None:
        # Existing clearances are immutable. Adopt their exact selection in a
        # separate versioned record without changing archived model requests.
        saved = capture(journal, run_id, clearance)
    if (
        saved.get("contract") != VERSION
        or saved.get("run_id") != str(run_id)
        or saved.get("clearance_ref") != clearance_ref(run_id).model_dump(mode="json")
        or saved.get("clearance_hash") != digest(clearance)
        or saved.get("candidate_plan_ref") != clearance["candidate_plan_ref"]
        or saved.get("plan_review_ref") != clearance["plan_review_ref"]
        or clearance.get("run_id") != str(run_id)
        or clearance.get("decision") != "accept"
    ):
        raise ContractError("research_annex_plan_binding_changed")
    return saved["selections_by_plan"]


def capture(journal, run_id, clearance):
    from .planning import clearance_ref, verify_clearance

    existing = _read_optional(journal, selection_ref(run_id))
    if existing is not None:
        if existing["clearance_hash"] != digest(clearance):
            raise ContractError("research_annex_plan_binding_changed")
        return existing
    if verify_clearance(journal, run_id) != clearance:
        raise ContractError("research_annex_plan_binding_changed")
    rows = []
    for stage in ("plan", "plan-revise", "plan-review", "plan-review-revised"):
        row = journal.invocation(run_id, stage)
        if row is not None and row["status"] == "completed":
            rows.append((stage, row, journal.output(row)))
    accepted = {
        record["candidate_plan_ref"]["record_id"]: record["candidate_plan_hash"]
        for stage, _, record in rows
        if stage.startswith("plan-review") and record["output"].get("decision") == "accept"
    }
    selections, evidence = {}, []
    for stage, row, record in rows:
        if stage.startswith("plan-review"):
            if record["output"].get("decision") != "accept":
                continue
            plan_hash = record["candidate_plan_hash"]
        else:
            if str(row["output_record_id"]) not in accepted:
                continue
            plan_hash = digest(record["output"])
            if accepted[str(row["output_record_id"])] != plan_hash:
                raise ContractError("research_annex_plan_binding_changed")
        selected = selections.setdefault(plan_hash, [])
        for ancestor in lineage(journal, row):
            for event in journal.events(ancestor):
                if event["event_kind"] != "source_tool_result":
                    continue
                ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
                receipt = journal.read(ref)
                data = receipt["data"]
                if data["name"] == "select_research_result" and data["result"].get("selected"):
                    selected.append(
                        dict(result_id=data["arguments"]["result_id"], reason=data["arguments"]["reason"])
                    )
                    # Private event aliases are audit identities, not generic
                    # runtime dependencies. The accepted semantic plan/review
                    # own execution provenance and fresh access checks.
                    evidence.append(dict(event_id=str(ref.record_id), sha256=digest(receipt)))
    saved = dict(
        contract=VERSION,
        run_id=str(run_id),
        clearance_ref=clearance_ref(run_id).model_dump(mode="json"),
        clearance_hash=digest(clearance),
        candidate_plan_ref=clearance["candidate_plan_ref"],
        plan_review_ref=clearance["plan_review_ref"],
        selections_by_plan=selections,
        selection_event_receipts=evidence,
    )
    journal.record(saved, record_id=selection_ref(run_id).record_id)
    return saved
