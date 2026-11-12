import json
from copy import deepcopy
from uuid import uuid4

import pytest

from podgen.audio.assembly.pcm import read
from podgen.audio.http import HttpReceipt
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.exchange_history import attempts
from podgen.runtime.evaluation.audio.local_replacement.apply import apply
from podgen.runtime.evaluation.audio.local_replacement.approval import approve
from podgen.runtime.evaluation.audio.local_replacement.contracts import prefix
from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
from podgen.runtime.evaluation.audio.local_replacement.service import reprocess, synthesize
from tests.audio_local_replacement_fixtures import Synthesis, allow, fixture


def test_prepare_selects_full_sentence_short_request_and_requires_exact_human_approval(monkeypatch):
    j, config, _ = fixture(monkeypatch)
    proposal = prepare(j, config)
    p = proposal["payload"]
    assert p["text"] == "Второй текст."
    assert p["synthesis_input"]["utterances"][0]["text"] == "Второй текст."
    assert p["request"]["body"]["input"][0]["content"][0]["text"] == "Второй текст."
    assert p["profile"]["synthesis"]["voice_id"] == "fixture-gemini"
    assert p["error_source"] == "human"
    transport = Synthesis()
    with pytest.raises(ContractError, match="requires_explicit"):
        synthesize(j, config.proposal_id, transport)
    assert not transport.calls
    with pytest.raises(ContractError, match="hash_mismatch"):
        approve(
            j,
            config.proposal_id,
            dict(
                proposal_hash="0" * 64,
                reason="wrong range",
                reservation_per_request_usd="0.50",
                max_cost_usd="1",
            ),
        )
    assert not transport.calls


def test_revision_shift_expand_preserves_original_and_does_not_inherit_approval(monkeypatch):
    j, config, _ = fixture(monkeypatch)
    old = prepare(j, config)
    allow(j, config)
    revised = config.model_copy(
        update=dict(
            proposal_id=uuid4(),
            sentence_offset=-1,
            sentence_count=2,
            parent_proposal_ref=j.ref(prefix(config.proposal_id) + ":proposal"),
        )
    )
    current = prepare(j, revised)
    assert current["payload"]["text"] == "Первый текст.\nВторой текст."
    assert current["proposal_hash"] != old["proposal_hash"]
    assert j.get(prefix(config.proposal_id) + ":proposal") == old
    with pytest.raises(ContractError, match="requires_explicit"):
        synthesize(j, revised.proposal_id, Synthesis())


def test_apply_preserves_outside_pcm_and_shifts_timeline_then_repairs_new_group(monkeypatch):
    j, config, original = fixture(monkeypatch)
    proposal = prepare(j, config)
    allow(j, config)
    frozen = deepcopy(j.records)
    speech = Synthesis()
    assert synthesize(j, config.proposal_id, speech) == "synthesized"
    assert synthesize(j, config.proposal_id, speech) == "synthesized" and len(speech.calls) == 1
    ref = RuntimeRef.model_validate(apply(j, [config.proposal_id]))
    result = j.base.read(ref)
    master = j.base.read_blob(ref, BlobRef.model_validate(result["master_pcm_blob"]))
    params, frames, pcm = read(master)
    _, original_frames, old_pcm = read(original)
    a, b = proposal["payload"]["frame_range"]["start_frame"], proposal["payload"]["frame_range"]["end_frame"]
    edit = result["edits"][0]
    assert pcm[: a * params[1]] == old_pcm[: a * params[1]]
    assert pcm[edit["new_end_frame"] * params[1] :] == old_pcm[b * params[1] :]
    assert frames == original_frames + edit["delta_frames"]
    assert all(j.records[k] == v for k, v in frozen.items())
    timeline = json.loads(j.base.read_blob(ref, BlobRef.model_validate(result["timeline_blob"])))
    prior = json.loads(
        j.bytes(prefix(config.proposal_id) + ":proposal", proposal["payload"]["timeline_blob"])
    )
    for before in prior["word_segments"]:
        if before["start_frame"] >= b:
            after = next(
                row for row in timeline["word_segments"] if row["segment_index"] == before["segment_index"]
            )
            assert after["start_frame"] == before["start_frame"] + edit["delta_frames"]
    assert not any(a <= word["start_frame"] < edit["new_end_frame"] for word in timeline["word_segments"])
    assert len(result["joins"]) == 2 and not result["splice_proof"]["global_normalization"]
    follow = config.model_copy(
        update=dict(
            proposal_id=uuid4(),
            parent_subject=config.parent_subject.model_copy(update=dict(kind="local_master", ref=ref)),
            recognition_audit_id=None,
            approximate_seconds=(edit["new_start_frame"] + edit["new_end_frame"]) / 2 / params[2],
        )
    )
    next_proposal = prepare(j, follow)
    assert next_proposal["payload"]["text"] == "Второй текст."
    assert next_proposal["payload"]["selected_units"][0]["timing_basis"] == "replacement_audio_group"
    with pytest.raises(ContractError, match="stale_parent"):
        apply(j, [config.proposal_id], parent_subject=follow.parent_subject)
    allow(j, follow)
    assert synthesize(j, follow.proposal_id, Synthesis(seconds=1)) == "synthesized"
    second = j.base.read(RuntimeRef.model_validate(apply(j, [follow.proposal_id])))
    assert second["parent_subject"] == follow.parent_subject.model_dump(mode="json")
    assert second["parent_audio_sha256"] == result["master_pcm_blob"]["sha256"]


@pytest.mark.parametrize(
    "receipt",
    [
        HttpReceipt(520, {}, b"unknown server failure", 1),
        HttpReceipt(503, {}, b"unknown", 1),
        HttpReceipt(409, {}, b"possibly pending", 1),
        HttpReceipt(200, {}, b"partial", 1, False, "ReadTimeout"),
    ],
)
def test_unknown_delivery_does_not_repeat_or_advance(monkeypatch, receipt):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    speech = Synthesis(receipt)
    assert synthesize(j, c.proposal_id, speech) == "outcome_unknown"
    assert synthesize(j, c.proposal_id, speech) == "outcome_unknown"
    with pytest.raises(ContractError, match="known_rejection"):
        synthesize(j, c.proposal_id, speech, retry_reason="Cannot claim safe rejection")
    assert len(speech.calls) == 1


def test_lost_receipt_and_local_reprocess_are_idempotent(monkeypatch):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    lost = Synthesis(lost=True)
    with pytest.raises(RuntimeError):
        synthesize(j, c.proposal_id, lost)
    assert synthesize(j, c.proposal_id, lost) == "outcome_unknown" and len(lost.calls) == 1
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    speech = Synthesis()
    synthesize(j, c.proposal_id, speech)
    assert reprocess(j, c.proposal_id, "Re-read saved lossless receipt") == "synthesized"
    assert len(speech.calls) == 1


def test_known_rejection_retry_and_budget_preserve_history(monkeypatch):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    denied = Synthesis(HttpReceipt(429, {}, b"rate limit", 1))
    assert synthesize(j, c.proposal_id, denied) == "provider_rejected"
    good = Synthesis()
    assert synthesize(j, c.proposal_id, good, retry_reason="Explicit retry after rate limit") == "synthesized"
    assert len(attempts(j, prefix(c.proposal_id))) == 2
    assert denied.calls[0] == good.calls[0]


def test_batch_apply_two_approved_ranges_and_reversed_replay_are_one_version(monkeypatch):
    j, c, old = fixture(monkeypatch)
    first = c.model_copy(update=dict(proposal_id=uuid4(), approximate_seconds=0.5))
    last = c.model_copy(update=dict(proposal_id=uuid4(), approximate_seconds=6.5))
    for config in (first, last):
        allow(j, config)
        assert synthesize(j, config.proposal_id, Synthesis(seconds=1)) == "synthesized"
    ref = apply(j, [last.proposal_id, first.proposal_id])
    assert apply(j, [first.proposal_id, last.proposal_id]) == ref
    result = j.base.read(RuntimeRef.model_validate(ref))
    master = j.base.read_blob(
        RuntimeRef.model_validate(ref), BlobRef.model_validate(result["master_pcm_blob"])
    )
    params, frames, pcm = read(master)
    original = read(old)[2]
    stride = params[1]
    for row in result["frame_map"]:
        if row["kind"] == "parent_audio":
            assert (
                pcm[row["master_start_frame"] * stride : row["master_end_frame"] * stride]
                == original[row["source_start_frame"] * stride : row["source_end_frame"] * stride]
            )
    assert len(result["edits"]) == 2 and len(result["joins"]) == 4
    assert result["frame_map"][-1]["master_end_frame"] == frames


def test_budget_and_parent_byte_hash_block_before_paid_call(monkeypatch):
    j, c, _ = fixture(monkeypatch)
    allow(j, c, max_requests=1)
    denied = Synthesis(HttpReceipt(429, {}, b"refused", 1))
    synthesize(j, c.proposal_id, denied)
    assert synthesize(j, c.proposal_id, denied, retry_reason="Explicit attempt") == "budget_exhausted"
    assert len(denied.calls) == 1
    j, c, _ = fixture(monkeypatch)
    proposal = prepare(j, c)
    allow(j, c)
    blob = proposal["payload"]["parent_audio_blob"]
    j.blobs[blob["storage_key"]] = j.blobs[blob["storage_key"]][:-1] + b"x"
    speech = Synthesis()
    with pytest.raises(ContractError, match="parent_audio_changed_before_submit"):
        synthesize(j, c.proposal_id, speech)
    assert not speech.calls
