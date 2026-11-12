import os
from decimal import Decimal

from podgen.domain.errors import ContractError

from .contracts import prefix
from .deliveries import RetryPolicy, deliveries, latest, reserved
from .prepare import prepare
from .processing import process, revisions
from .submission import submit


def state(journal, config):
    delivery = latest(journal, config)
    key = delivery["key"]
    receipts = delivery["receipt"]
    processed = [
        r
        for r in revisions(journal, config.audit_id)
        if r["prepared"]["source_receipt_ref"] == journal.ref(key + ":receipt").model_dump(mode="json")
    ]
    if processed and processed[-1]["result"]:
        return "candidates_generated"
    if processed and processed[-1]["failure"]:
        return processed[-1]["failure"]["status"]
    if receipts:
        if not receipts["complete"] or receipts["error"]:
            return "outcome_unknown"
        if not 200 <= receipts["status_code"] < 300:
            return "provider_rejected"
        return "receipt_saved"
    return "outcome_unknown" if journal.get(key + ":submit") else "prepared"


def run(journal, config, transport, *, retry_policy=None):
    with journal.lock():
        manifest = prepare(journal, config)
        root = prefix(config.audit_id)
        if retry_policy is not None:
            policy = RetryPolicy.model_validate(retry_policy)
            old = latest(journal, config)
            if (
                not policy.reason.strip()
                or not old["receipt"]
                or not old["receipt"]["complete"]
                or old["receipt"]["error"]
                or 200 <= old["receipt"]["status_code"] < 300
            ):
                raise ContractError("asr_retry_requires_complete_provider_rejection")
            count, cost = reserved(journal, config)
            if count >= policy.max_total_requests or cost + Decimal(config.reservation_usd) > Decimal(
                policy.max_total_cost_usd
            ):
                return "budget_exhausted"
            key = f"{root}:retry:{len(deliveries(journal, config))}"
            journal.put(
                key + ":authorization",
                dict(
                    contract="audio-asr-retry-authorization/1",
                    policy=policy.model_dump(mode="json"),
                    parent_receipt_ref=journal.ref(old["key"] + ":receipt").model_dump(mode="json"),
                ),
            )
        delivery = latest(journal, config)
        key = delivery["key"]
        current = state(journal, config)
        if current not in ("prepared", "receipt_saved"):
            return current
        if current == "prepared":
            credential = os.environ.get(config.credential_env)
            if not credential:
                return "credential_missing"
            submitted, cost = reserved(journal, config)
            policy = (
                delivery["authorization"]["policy"]
                if delivery["authorization"]
                else dict(max_total_requests=config.max_requests, max_total_cost_usd=config.max_cost_usd)
            )
            if submitted >= policy["max_total_requests"] or cost + Decimal(config.reservation_usd) > Decimal(
                policy["max_total_cost_usd"]
            ):
                return "budget_exhausted"
            submit(journal, config, manifest, delivery, submitted, policy, credential, transport)
            current = state(journal, config)
            if current != "receipt_saved":
                return current
        return process(journal, config, manifest)


def reprocess(journal, config, reason):
    if not reason or not reason.strip():
        raise ContractError("fidelity_reprocess_reason_required")
    with journal.lock():
        manifest = prepare(journal, config)
        receipt = latest(journal, config)["receipt"]
        if (
            not receipt
            or not receipt["complete"]
            or receipt["error"]
            or not 200 <= receipt["status_code"] < 300
        ):
            raise ContractError("fidelity_reprocess_requires_complete_success_receipt")
        return process(journal, config, manifest, reason=reason)
