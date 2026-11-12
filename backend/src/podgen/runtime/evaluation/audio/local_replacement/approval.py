from datetime import datetime, timezone

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .contracts import Approval, ApprovalV2, contextual, prefix


def approve(journal, proposal_id, value):
    with journal.lock():
        key = prefix(proposal_id)
        proposal = verify_proposal(journal, proposal_id)
        approval = (ApprovalV2 if contextual(proposal["payload"]) else Approval).model_validate(value)
        if proposal["proposal_hash"] != approval.proposal_hash:
            raise ContractError("local_replacement_approval_hash_mismatch")
        saved = journal.get(key + ":approval")
        if saved:
            if saved["approval"] != approval.model_dump(mode="json"):
                raise ContractError("local_replacement_approval_already_frozen")
            return saved
        record = dict(
            contract="audio-local-replacement-human-approval/1",
            source="human",
            proposal_ref=journal.ref(key + ":proposal").model_dump(mode="json"),
            approval=approval.model_dump(mode="json"),
            actor_id=str(journal.repo.actor_id),
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        journal.put(key + ":approval", record)
        return record


def verify_proposal(journal, proposal_id):
    record = journal.get(prefix(proposal_id) + ":proposal")
    if not record or digest(record["payload"]) != record["proposal_hash"]:
        raise ContractError("local_replacement_proposal_missing_or_hash_changed")
    return record


def authorized(journal, proposal_id):
    proposal = verify_proposal(journal, proposal_id)
    approval = journal.get(prefix(proposal_id) + ":approval")
    if (
        not approval
        or approval["source"] != "human"
        or approval["approval"]["proposal_hash"] != proposal["proposal_hash"]
    ):
        raise ContractError("local_replacement_requires_explicit_exact_proposal_approval")
    return proposal, (ApprovalV2 if contextual(proposal["payload"]) else Approval).model_validate(
        approval["approval"]
    )
