from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef

from ..adjudication.contracts import prefix as review_prefix
from ..fidelity.contracts import FidelityConfigV2
from ..fidelity.contracts import prefix as fidelity_prefix
from ..fidelity.prepare import prepare as prepare_fidelity
from .contracts import prefix
from .replacement import build_replacement


def prepare_verification(journal, config, result_ref):
    replacement = build_replacement(journal, config, result_ref)
    review = journal.get(review_prefix(config.review_id) + ":manifest")
    fidelity = journal.base.read(RuntimeRef.model_validate(review["fidelity_manifest_ref"]))
    previous = dict(fidelity["configuration"])
    for field in ("contract", "subject", "result_ref", "audit_id"):
        previous.pop(field, None)
    followup = FidelityConfigV2(
        audit_id=uuid5(config.repair_id, "asr:" + replacement["assembly_ref"]["record_id"]),
        subject=dict(kind="assembly", ref=replacement["assembly_ref"]),
        **previous,
    )
    with journal.lock():
        prepare_fidelity(journal, followup)
        outcome = dict(
            replacement,
            verification_configuration=followup.model_dump(mode="json"),
            verification_manifest_ref=journal.ref(
                fidelity_prefix(followup.audit_id) + ":manifest"
            ).model_dump(mode="json"),
            contract="fragment-repair-outcome/1",
            status="verification_required",
            next_steps=[
                "run_new_master_asr",
                "review_new_candidates_against_actual_audio",
                "review_actual_join_windows",
            ],
            production_ready=False,
        )
        journal.put(prefix(config.repair_id) + ":outcome:" + str(result_ref.record_id), outcome)
    return outcome
