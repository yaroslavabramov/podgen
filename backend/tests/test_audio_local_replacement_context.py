from copy import deepcopy

import pytest

from podgen.audio.http import HttpReceipt
from podgen.audio.synthesis.contracts import PreparedRequest
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.local_replacement import localization
from podgen.runtime.evaluation.audio.local_replacement.approval import approve
from podgen.runtime.evaluation.audio.local_replacement.contracts import Approval, ProposalConfig, prefix
from podgen.runtime.evaluation.audio.local_replacement.export import export_proposal
from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
from podgen.runtime.evaluation.audio.local_replacement.service import reprocess, synthesize
from tests.audio_local_replacement_fixtures import Recognition, Synthesis, allow, fixture, localize


def test_single_approval_separate_budgets_and_no_second_tts_after_asr_failure(monkeypatch, tmp_path):
    j, c, _ = fixture(monkeypatch)
    proposal = prepare(j, c)
    with pytest.raises(ValueError, match="localization_budget"):
        approve(
            j,
            c.proposal_id,
            Approval(
                proposal_hash=proposal["proposal_hash"],
                reason="Exact approval",
                reservation_per_request_usd="1",
                max_cost_usd="1",
            ).model_dump(),
        )
    allow(j, c)
    tts = Synthesis()
    assert synthesize(j, c.proposal_id, tts) == "synthesized"
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    asr = Recognition(proposal["payload"]["synthesis_input"]["utterances"])
    assert localization.run(j, c.proposal_id, asr) == "asr_credential_missing"
    assert not asr.calls
    monkeypatch.setenv("FISH_API_KEY", "fixture")
    assert localization.run(j, c.proposal_id, asr) == "extraction_ready"
    assert synthesize(j, c.proposal_id, tts) == "synthesized"
    assert localization.run(j, c.proposal_id, asr) == "extraction_ready"
    assert reprocess(j, c.proposal_id, "Decode saved full context") == "synthesized"
    assert localization.run(j, c.proposal_id, asr) == "extraction_ready"
    assert len(tts.calls) == len(asr.calls) == 1
    status = export_proposal(j, c.proposal_id, tmp_path)
    assert status["delivery_state"] == "extraction_ready"
    assert status["localization"]["requests_submitted"] == 1
    assert list(tmp_path.glob("private/*/asr-wire.bin"))
    assert list(tmp_path.glob("private/*/middle.wav"))
    page = (tmp_path / "index.html").read_text()
    assert "Первый текст. Второй текст. Третий текст." in page
    assert "В запись вставится только выбранный фрагмент" in page


@pytest.mark.parametrize(
    "receipt",
    [HttpReceipt(503, {}, b"unknown", 1), HttpReceipt(200, {}, b"partial", 1, False, "ReadTimeout")],
)
def test_asr_unknown_never_retries_or_repays_tts(monkeypatch, receipt):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    tts = Synthesis()
    synthesize(j, c.proposal_id, tts)
    result, asr = localize(j, c, monkeypatch, receipt=receipt)
    assert result == "asr_outcome_unknown"
    assert localization.run(j, c.proposal_id, asr) == "asr_outcome_unknown"
    with pytest.raises(ContractError, match="known_rejection"):
        localization.run(j, c.proposal_id, asr, retry_reason="Cannot guess safe retry")
    synthesize(j, c.proposal_id, tts)
    assert len(tts.calls) == len(asr.calls) == 1


def test_asr_lost_ack_no_resubmit_and_known_rejection_budget(monkeypatch):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    synthesize(j, c.proposal_id, Synthesis())
    with pytest.raises(RuntimeError):
        localize(j, c, monkeypatch, lost=True)
    _, _, _, root = localization.binding(j, c.proposal_id)
    assert j.get(root + ":attempt:1:submit") and not j.get(root + ":attempt:1:receipt")
    assert localization.current(j, c.proposal_id) == "asr_outcome_unknown"
    j, c, _ = fixture(monkeypatch)
    allow(
        j,
        c,
        localization_budget=dict(reservation_per_request_usd="0.01", max_cost_usd="0.02", max_requests=2),
    )
    synthesize(j, c.proposal_id, Synthesis())
    result, asr = localize(j, c, monkeypatch, receipt=HttpReceipt(429, {}, b"reject", 1))
    assert result == "asr_provider_rejected"
    assert (
        localization.run(j, c.proposal_id, asr, retry_reason="Human retries rejected ASR")
        == "asr_provider_rejected"
    )
    assert (
        localization.run(j, c.proposal_id, asr, retry_reason="No remaining budget") == "asr_budget_exhausted"
    )
    assert len(asr.calls) == 2


def test_failed_localization_reprocesses_saved_receipt_only(monkeypatch):
    j, c, _ = fixture(monkeypatch)
    allow(j, c)
    synthesize(j, c.proposal_id, Synthesis())
    result, asr = localize(j, c, monkeypatch, receipt=HttpReceipt(200, {}, b"{}", 1))
    assert result == "localization_failed"
    assert localization.run(j, c.proposal_id, asr) == "localization_failed"
    assert (
        localization.run(j, c.proposal_id, asr, reprocess_reason="Read saved ASR again")
        == "localization_failed"
    )
    assert len(asr.calls) == 1


def test_legacy_frozen_proposal_hash_export_and_no_fresh_submit(monkeypatch, tmp_path):
    j, c, _ = fixture(monkeypatch)
    raw = c.model_dump(mode="json")
    del raw["localization"]
    raw["contract"] = "audio-local-replacement-proposal/1"
    old = ProposalConfig.model_validate(raw)
    with pytest.raises(ContractError, match="legacy_prepare_requires_contextual_revision"):
        prepare(j, old)
    # Seed a historical record: the production prepare path cannot create /1.
    original = prepare(j, c)
    payload = original["payload"]
    payload["contract"] = "audio-local-replacement-draft/1"
    payload["configuration"] = old.model_dump(mode="json")
    from uuid import uuid5

    from podgen.audio.synthesis.contracts import SynthesisInput, SynthesisProfile, SynthesisUtterance
    from podgen.audio.synthesis.registry import adapter
    from podgen.audio.synthesis.transport import encode_request
    from podgen.domain.canonical import digest

    payload["configuration_hash"] = digest(payload["configuration"])
    del payload["context_selection"], payload["localization"]
    short = SynthesisInput(
        utterances=[
            SynthesisUtterance(
                utterance_id=uuid5(c.proposal_id, "sentence:0"), speaker="HOST", text=payload["text"]
            )
        ]
    )
    request = adapter("gemini").prepare(
        SynthesisProfile.model_validate(payload["profile"]["synthesis"]), short
    )
    payload["synthesis_input"] = short.model_dump(mode="json")
    payload["request"] = request.model_dump(mode="json")
    payload["wire_blob"] = j.blob(encode_request(request), "application/json")
    original["proposal_hash"] = digest(payload)
    j.records[str(j.ref(prefix(old.proposal_id) + ":proposal").record_id)] = deepcopy(original)
    assert prepare(j, old) == original
    frozen = deepcopy(j.records)
    approve(
        j,
        old.proposal_id,
        Approval(
            proposal_hash=original["proposal_hash"],
            reason="Historical approval",
            reservation_per_request_usd="1",
            max_cost_usd="1",
        ),
    )
    speech = Synthesis()
    assert synthesize(j, old.proposal_id, speech) == "legacy_proposal_requires_contextual_revision"
    assert not speech.calls
    assert prepare(j, old) == original
    export_proposal(j, old.proposal_id, tmp_path)
    assert all(j.records[k] == v for k, v in frozen.items())
    # Historical successful receipts still decode/export locally under their exact /1 wire.
    root = prefix(old.proposal_id)
    key = root + ":attempt:1"
    response = Synthesis().submit(PreparedRequest.model_validate(original["payload"]["request"]), "unused")
    j.put(key + ":prepared", dict(historical=True))
    j.put(key + ":submit", dict(reservation_usd="1"))
    j.put(
        key + ":receipt",
        dict(
            status_code=response.status_code,
            headers=response.headers,
            elapsed_ms=response.elapsed_ms,
            complete=True,
            error=None,
            raw_response_blob=j.blob(response.body, "application/json"),
        ),
    )
    assert reprocess(j, old.proposal_id, "Read historical receipt") == "synthesized"
    assert synthesize(j, old.proposal_id, speech) == "synthesized"
    assert not speech.calls
    assert prepare(j, old) == original
    export_proposal(j, old.proposal_id, tmp_path)


def test_asr_budget_covers_new_pcm_hash_after_explicit_tts_reprocess(monkeypatch):
    from podgen.runtime.evaluation.audio.local_replacement import processing
    from podgen.runtime.evaluation.audio.local_replacement.localization_state import proposal_ledger

    j, c, _ = fixture(monkeypatch)
    allow(
        j,
        c,
        localization_budget=dict(reservation_per_request_usd="0.01", max_cost_usd="0.01", max_requests=1),
    )
    speech = Synthesis()
    assert synthesize(j, c.proposal_id, speech) == "synthesized"
    result, asr = localize(j, c, monkeypatch)
    assert result == "extraction_ready"
    _, _, _, old_root = localization.binding(j, c.proposal_id)
    original_decode = processing.decode

    def corrected_decode(audio):
        decoded, metrics = original_decode(audio)
        return decoded[:-1] + bytes([decoded[-1] ^ 1]), metrics

    monkeypatch.setattr(processing, "decode", corrected_decode)
    assert reprocess(j, c.proposal_id, "Explicit decoder correction") == "synthesized"
    _, _, _, new_root = localization.binding(j, c.proposal_id)
    assert old_root != new_root
    assert localization.run(j, c.proposal_id, asr) == "asr_budget_exhausted"
    assert proposal_ledger(j, c.proposal_id)[0] == 1
    assert len(speech.calls) == len(asr.calls) == 1


def finding_config(config, candidate_id):
    from uuid import uuid4

    from podgen.runtime.evaluation.audio.local_replacement.contracts import ProposalConfigV3

    values = config.model_dump(mode="json", exclude={"contract", "approximate_seconds"})
    return ProposalConfigV3(**dict(values, proposal_id=str(uuid4()), candidate_id=candidate_id))


def test_a_finding_selects_the_sentence_holding_its_words_not_a_time(monkeypatch):
    from uuid import uuid4

    journal, config, _ = fixture(monkeypatch)
    root = "fidelity:" + str(config.recognition_audit_id) + ":processing:2"
    words = [dict(index=2, text="Второй", normalized="второй", char_start=14, char_end=20, utterance_index=0)]
    candidate = dict(
        kind="script_discrepancy",
        operation="replace",
        candidate_id=str(uuid4()),
        expected_tokens=words,
        recognized_tokens=[dict(text="Вторник")],
        expected_token_range=[2, 3],
    )
    journal.put(root, {})
    journal.put(root + ":recognition", journal.get(root[: -len("2")] + "1:recognition"))
    journal.put(root + ":result", dict(report=dict(expected_tokens=words, candidates=[candidate])))
    proposal = prepare(journal, finding_config(config, candidate["candidate_id"]))
    assert proposal["payload"]["text"] == "Второй текст."
    assert proposal["payload"]["finding"]["candidate_id"] == candidate["candidate_id"]
    with pytest.raises(ContractError, match="local_replacement_unknown_finding"):
        prepare(journal, finding_config(config, str(uuid4())))


def test_an_inserted_finding_selects_the_sentence_at_its_script_position(monkeypatch):
    from uuid import uuid4

    journal, config, _ = fixture(monkeypatch)
    root = "fidelity:" + str(config.recognition_audit_id) + ":processing:2"
    tokens = [
        dict(index=i, text=w, normalized=w.casefold(), char_start=s, char_end=s + len(w), utterance_index=0)
        for i, (w, s) in enumerate(
            [("Первый", 0), ("текст", 7), ("Второй", 14), ("текст", 21), ("Третий", 28)]
        )
    ]
    candidate = dict(
        kind="script_discrepancy",
        operation="insert",
        candidate_id=str(uuid4()),
        expected_tokens=[],
        recognized_tokens=[dict(text="лишнее")],
        expected_token_range=[4, 4],
    )
    journal.put(root, {})
    journal.put(root + ":recognition", journal.get(root[: -len("2")] + "1:recognition"))
    journal.put(root + ":result", dict(report=dict(expected_tokens=tokens, candidates=[candidate])))
    proposal = prepare(journal, finding_config(config, candidate["candidate_id"]))
    assert proposal["payload"]["text"] == "Третий текст."


def spliced_check(journal, config):
    from podgen.runtime.evaluation.audio.local_replacement.localization import binding

    result = journal.get(binding(journal, config.proposal_id)[3] + ":result")
    assert result["contract"] == "audio-local-extraction/2"
    return result["target_check"]


def test_the_spliced_sentence_is_checked_against_its_script_text(monkeypatch):
    journal, config, _ = fixture(monkeypatch)
    allow(journal, config)
    assert synthesize(journal, config.proposal_id, Synthesis()) == "synthesized"
    assert localize(journal, config, monkeypatch)[0] == "extraction_ready"
    check = spliced_check(journal, config)
    assert check["heard_as_written"] and not check["discrepancies"]
    assert check["expected_text"] == check["recognized_text"] == "Второй текст"


def test_a_misheard_word_in_the_spliced_sentence_goes_to_a_person(monkeypatch):
    journal, config, _ = fixture(monkeypatch)
    allow(journal, config)
    assert synthesize(journal, config.proposal_id, Synthesis()) == "synthesized"
    proposal = prepare(journal, config)
    utterances = deepcopy(proposal["payload"]["synthesis_input"]["utterances"])
    for row in utterances:
        row["text"] = row["text"].replace("Второй", "Вторник")
    monkeypatch.setenv("FISH_API_KEY", "fixture-asr-key")
    assert localization.run(journal, config.proposal_id, Recognition(utterances)) == "extraction_ready"
    check = spliced_check(journal, config)
    assert not check["heard_as_written"]
    assert check["discrepancies"] == [
        dict(
            operation="replace",
            expected_text="Второй",
            recognized_text="Вторник",
            review_route="user_confirmation",
        )
    ]
