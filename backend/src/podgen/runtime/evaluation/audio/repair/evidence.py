from podgen.audio.fidelity.adjudication.contracts import WindowResolution
from podgen.domain.errors import ContractError

from ..adjudication.attempts import history
from ..adjudication.contracts import prefix as review_prefix
from ..adjudication.contracts import validate_config
from ..adjudication.fallback import superseded
from ..adjudication.prepare import subject as review_subject
from .localization import bind_fragment
from .parent import index


def verify(journal, config, part):
    parent, bindings, part_index = index(journal, config.parent_subject, config.part_source)
    review = journal.get(review_prefix(config.review_id) + ":manifest")
    if not review:
        raise ContractError("repair_requires_registered_audio_review")
    review_config = validate_config(review["configuration"])
    fidelity, candidates, _ = review_subject(journal, review_config)
    subject_ref = fidelity.get("subject_ref", fidelity.get("subject_result_ref"))
    if (
        subject_ref != config.parent_subject.ref.model_dump(mode="json")
        or fidelity.get("subject_kind", "comparison_result") != config.parent_subject.kind
        or fidelity["audio_blob"]["sha256"] != parent["audio_blob"]["sha256"]
    ):
        raise ContractError("repair_review_is_for_different_audio_subject")
    candidate = next(
        (c for c in candidates["report"]["candidates"] if c["candidate_id"] == config.candidate_id), None
    )
    if not candidate:
        raise ContractError("repair_candidate_not_in_reviewed_source")
    for item in review["windows"]:
        rows = history(journal, item["key"])
        if rows and superseded(journal, review_config, rows[-1]["key"]):
            continue
        latest = rows[-1]["processing"][-1] if rows and rows[-1]["processing"] else None
        if (
            not latest
            or not latest["result"]
            or journal.ref(latest["key"] + ":result") != config.review_result_ref
        ):
            continue
        window = journal.get(item["key"])
        extraction = window["extraction"]
        output = WindowResolution.model_validate(latest["result"]["output"]).validate_window(
            window["case_ids"], extraction["sample_frames"] / extraction["sample_rate_hz"]
        )
        decision = next(
            (d for d in output.results if window["case_aliases"][d.case_id] == config.candidate_id), None
        )
        if not decision or decision.status != "confirmed":
            raise ContractError("repair_requires_confirmed_acoustic_mismatch")
        wanted = {s["utterance_id"] for s in candidate["script_selectors"]}
        if not wanted:
            position = candidate["expected_token_range"][0]
            wanted = {
                t["utterance_id"]
                for t in candidates["report"]["expected_tokens"][max(0, position - 1) : position + 1]
            }
        available = {u["utterance_id"] for u in part["source"]["source"]["utterances"]}
        if not wanted & available:
            raise ContractError("repair_case_does_not_address_selected_fragment")
        localization = bind_fragment(parent, part_index, extraction, decision)
        return dict(
            localization=localization,
            review_manifest_ref=journal.ref(review_prefix(config.review_id) + ":manifest").model_dump(
                mode="json"
            ),
            review_result_ref=config.review_result_ref.model_dump(mode="json"),
            candidate_result_ref=review["source_candidate_result_ref"],
            candidate_id=config.candidate_id,
            decision=decision.model_dump(mode="json"),
            window_ref=item["ref"],
            parent_part_index=part_index,
            source_scope="whole_original_synthesis_fragment",
            complete_case_resolution="pending_new_audio_qa",
        )
    raise ContractError("repair_requires_latest_validated_review_result")
