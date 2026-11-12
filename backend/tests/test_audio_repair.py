import json
from uuid import uuid4

import pytest

from podgen.audio.fidelity.adjudication.contracts import CaseResolution
from podgen.audio.fidelity.tokens import tokens
from podgen.audio.http import HttpReceipt
from podgen.audio.synthesis.transport import encode_request
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.adjudication.attempts import history
from podgen.runtime.evaluation.audio.adjudication.prepare import prepare as review_prepare
from podgen.runtime.evaluation.audio.adjudication.service import run as review_run
from podgen.runtime.evaluation.audio.exchange_history import attempts
from podgen.runtime.evaluation.audio.fidelity.contracts import prefix as fidelity_prefix
from podgen.runtime.evaluation.audio.repair.contracts import RepairConfig, prefix
from podgen.runtime.evaluation.audio.repair.localization import bind_fragment
from podgen.runtime.evaluation.audio.repair.prepare import prepare
from podgen.runtime.evaluation.audio.repair.service import run
from tests.audio_cases import response, signal, source
from tests.test_audio_adjudication_runtime import review_fixture


class ConfirmedReviewer:
    def __init__(self, status="confirmed", reason="audible_mismatch"):
        self.status, self.reason = status, reason

    def submit(self, request, wire, credential):
        payload = json.loads(json.loads(wire)["input"][0]["text"])
        outputs = [
            dict(
                case_id=c["case_id"],
                status=self.status,
                reason_code=self.reason,
                heard_spans=[
                    dict(
                        start_seconds=0.1,
                        end_seconds=min(0.5, payload["audio_window"]["duration_seconds"] / 2),
                        words="услышанные слова",
                    )
                ],
                explanation="Механическая fixture, не акустический ground truth.",
            )
            for c in payload["cases"]
        ]
        return HttpReceipt(
            200,
            {},
            json.dumps(
                dict(
                    status="completed",
                    steps=[
                        dict(
                            type="model_output",
                            content=[dict(type="text", text=json.dumps(dict(results=outputs)))],
                        )
                    ],
                )
            ).encode(),
            5,
        )


def fixture(monkeypatch, status="confirmed", reason="audible_mismatch", asr_response=None):
    journal, review = review_fixture(monkeypatch, asr_response=asr_response)
    review_run(journal, review, ConfirmedReviewer(status, reason))
    manifest = review_prepare(journal, review)
    window = manifest["windows"][0]
    row = history(journal, window["key"])[-1]
    result = journal.ref(row["processing"][-1]["key"] + ":result")
    fid = journal.get(fidelity_prefix(review.fidelity_audit_id) + ":manifest")
    parent = fid["subject_ref"]
    config = RepairConfig(
        repair_id=uuid4(),
        comparison_run_id=journal.run_id,
        parent_subject=dict(kind="comparison_result", ref=parent),
        part_source=dict(kind="comparison_result", ref=parent),
        review_id=review.review_id,
        review_result_ref=result,
        candidate_id=next(iter(journal.get(window["key"])["case_aliases"].values())),
        reason="Confirmed words differ; regenerate the original complete fragment",
        reservation_per_request_usd="0.50",
        max_cost_usd="1",
        max_requests=2,
    )
    return journal, config


class Synthesis:
    def __init__(self, reply=None, lost=False):
        self.calls = []
        self.reply = reply
        self.lost = lost

    def submit(self, request, credential):
        self.calls.append(encode_request(request))
        if self.lost:
            raise RuntimeError("lost acknowledgement")
        return self.reply if self.reply is not None else response(request.provider, signal(seconds=6))


def test_only_registered_confirmed_audio_evidence_can_authorize_repair(monkeypatch):
    for status, reason in (("dismissed", "asr_error"), ("inconclusive", "unclear_speech")):
        j, c = fixture(monkeypatch, status, reason)
        with pytest.raises(ContractError, match="requires_confirmed"):
            prepare(j, c)
        assert not attempts(j, prefix(c.repair_id))
    j, c = fixture(monkeypatch)
    with pytest.raises(ContractError):
        prepare(j, c.model_copy(update=dict(review_id=uuid4())))


def test_representation_case_is_allowed_only_after_confirmed_audible_mismatch(monkeypatch):
    text = source().utterances[0].text
    segments = [dict(text=t["text"], start=i * 0.5, end=i * 0.5 + 0.3) for i, t in enumerate(tokens(text))]
    segments[1]["text"] = "иная"
    j, c = fixture(
        monkeypatch,
        asr_response=dict(text=text, duration=5, segments=segments, request_id="representation-fixture"),
    )
    assert prepare(j, c)["evidence"]["decision"]["status"] == "confirmed"


def test_frozen_full_fragment_request_resume_and_bounded_retry(monkeypatch):
    j, c = fixture(monkeypatch)
    manifest = prepare(j, c)
    monkeypatch.setattr("podgen.runtime.evaluation.audio.repair.service.prepare_verification", lambda *a: {})
    sent = Synthesis(HttpReceipt(503, {}, b'{"error":"service"}', 5))
    assert run(j, c, sent) == "provider_rejected"
    assert run(j, c, sent) == "provider_rejected" and len(sent.calls) == 1
    actual = Synthesis()
    assert (
        run(j, c, actual, retry_reason="Explicit retry after provider recovered") == "verification_required"
    )
    rows = attempts(j, prefix(c.repair_id))
    assert len(rows) == 2 and rows[1]["prepared"]["parent_attempt_ref"] == j.ref(
        rows[0]["key"] + ":prepared"
    ).model_dump(mode="json")
    assert (
        actual.calls[0] == sent.calls[0] == j.bytes(prefix(c.repair_id) + ":manifest", manifest["wire_blob"])
    )
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert run(j, c, actual) == "verification_required" and len(actual.calls) == 1
    assert rows[1]["processing"][-1]["result"]["asr_and_audio_review"] == "required_after_reassembly"


def test_unknown_delivery_never_resubmits_and_missing_key_costs_nothing(monkeypatch):
    j, c = fixture(monkeypatch)
    transport = Synthesis(lost=True)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert run(j, c, transport) == "credential_missing" and not transport.calls
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "fixture")
    with pytest.raises(RuntimeError):
        run(j, c, transport)
    assert run(j, c, transport) == "outcome_unknown" and len(transport.calls) == 1
    assert attempts(j, prefix(c.repair_id))[0]["receipt"] is None


def test_cross_join_source_ids_do_not_allow_wrong_side_or_ambiguous_whole_unit():
    parent = dict(
        kind="assembly",
        result=dict(
            metrics=dict(sample_rate_hz=10, sample_frames=200),
            frame_map=[
                dict(master_start_frame=0, master_end_frame=100),
                dict(master_start_frame=100, master_end_frame=200),
            ],
        ),
    )
    extraction = dict(start_frame=90, sample_frames=30, sample_rate_hz=10)

    def decision(first, last):
        return CaseResolution(
            case_id="C1",
            status="confirmed",
            reason_code="audible_mismatch",
            heard_spans=[dict(start_seconds=first, end_seconds=last, words="слова")],
            explanation="fixture",
        )

    evidence = decision(1.1, 1.8)
    assert bind_fragment(parent, 1, extraction, evidence)["heard_master_ranges"][0]["start_frame"] == 101
    with pytest.raises(ContractError, match="different_fragment"):
        bind_fragment(parent, 0, extraction, evidence)
    for target in (0, 1):
        with pytest.raises(ContractError, match="ambiguous_across_fragments"):
            bind_fragment(parent, target, extraction, decision(0.5, 1.5))


@pytest.mark.parametrize("request_limit,budget", [(1, "1"), (3, "0.50")])
def test_missing_key_and_budget_stop_are_exported_separately_from_last_delivery(
    monkeypatch, tmp_path, request_limit, budget
):
    from podgen.runtime.evaluation.audio.repair.export import export

    j, c = fixture(monkeypatch)
    transport = Synthesis()
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert run(j, c, transport) == "credential_missing" and not transport.calls
    status = export(j, c, tmp_path / "missing")
    assert (
        status["current_stop_reason"] == "credential_missing"
        and status["last_delivery_state"] == "not_submitted"
    )
    j2, c2 = fixture(monkeypatch)
    c2 = c2.model_copy(update=dict(max_requests=request_limit, max_cost_usd=budget))
    refused = Synthesis(HttpReceipt(503, {}, b"unavailable", 5))
    assert run(j2, c2, refused) == "provider_rejected"
    assert (
        run(j2, c2, refused, retry_reason="Explicit retry requested") == "budget_exhausted"
        and len(refused.calls) == 1
    )
    status = export(j2, c2, tmp_path / "budget")
    assert (
        status["current_stop_reason"] == "budget_exhausted"
        and status["last_delivery_state"] == "provider_rejected"
    )
    assert status["latest_operation"]["requests_submitted"] == 1


def test_missing_key_stop_does_not_leak_into_new_unknown_delivery(monkeypatch, tmp_path):
    from podgen.runtime.evaluation.audio.repair.export import export

    j, c = fixture(monkeypatch)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    transport = Synthesis(lost=True)
    assert run(j, c, transport) == "credential_missing"
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "fixture")
    with pytest.raises(RuntimeError):
        run(j, c, transport)
    status = export(j, c, tmp_path)
    assert status["status"] == status["last_delivery_state"] == "outcome_unknown"
    assert status["current_stop_reason"] is None and not status["operation_applies_to_current_result"]
    assert len(transport.calls) == 1
