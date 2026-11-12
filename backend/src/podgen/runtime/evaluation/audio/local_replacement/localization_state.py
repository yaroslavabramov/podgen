"""Resolve the current full-context take and durable localization status."""

from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from .approval import authorized
from .attempts import state
from .contracts import contextual, prefix


def binding(journal, proposal_id):
    proposal, approval = authorized(journal, proposal_id)
    if not contextual(proposal["payload"]):
        raise ContractError("local_replacement_localization_requires_contextual_proposal")
    rows = attempts(journal, prefix(proposal_id))
    if not rows or state(journal, rows[-1]["key"]) != "synthesized":
        raise ContractError("local_replacement_localization_requires_synthesized_context")
    take = rows[-1]["processing"][-1]
    blob = take["result"]["decoded_pcm_blob"]
    key = prefix(proposal_id) + ":localization:" + blob["sha256"]
    return proposal, approval, take, key


def current(journal, proposal_id):
    _, _, _, root = binding(journal, proposal_id)
    if journal.get(root + ":result"):
        return "extraction_ready"
    if journal.get(root + ":failure"):
        return "localization_failed"
    rows = attempts(journal, root)
    return "asr_" + state(journal, rows[-1]["key"]) if rows else "asr_pending"


def proposal_ledger(journal, proposal_id):
    """Approval limits span every contextual PCM revision, while cache keys do not."""
    from decimal import Decimal

    from .attempts import ledger

    hashes = {
        revision["result"]["decoded_pcm_blob"]["sha256"]
        for attempt in attempts(journal, prefix(proposal_id))
        for revision in attempt["processing"]
        if revision["result"]
    }
    count, cost = 0, Decimal(0)
    for audio_hash in hashes:
        submitted, reserved = ledger(journal, prefix(proposal_id) + ":localization:" + audio_hash)
        count += submitted
        cost += reserved
    return count, cost
