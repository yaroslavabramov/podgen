from copy import deepcopy
from uuid import uuid4

import pytest
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio import results
from podgen.runtime.evaluation.audio.attempts import execute, history, ledger, prefix
from podgen.runtime.evaluation.audio.export import export
from podgen.runtime.evaluation.audio.reprocess import reprocess

from tests.audio_cases import MemoryJournal, config, response, signal, source


class Transport:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def submit(self, request, credential):
        self.calls += 1
        if self.fail:
            raise RuntimeError("acknowledgement lost")
        return response(request.provider, signal())


def fixture(monkeypatch):
    journal = MemoryJournal()
    settings = config(["elevenlabs", "gemini", "inworld", "fish", "minimax"])
    candidate = settings.profiles[0]
    speech = source()
    request = adapter("elevenlabs").prepare(candidate.synthesis, speech)
    src = journal.put(
        "source", dict(source=speech.model_dump(mode="json"), source_script_hash="test-binding")
    )
    cell = dict(
        cell_id=str(uuid4()),
        profile_id=candidate.profile_id,
        fragment_id="fragment-0",
        request=request.model_dump(mode="json"),
        source_ref=src.model_dump(mode="json"),
    )
    manifest = dict(
        configuration=settings.model_dump(mode="json"),
        configuration_hash="fixture-hash",
        cells=[cell],
        expected_samples=20,
        availability=[dict(provider="elevenlabs", configured_profiles=1)],
        source_refs=dict(fragment=src.model_dump(mode="json")),
    )
    journal.put("manifest", manifest)
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "not-persisted")
    return journal, settings, manifest, cell


def test_lost_acknowledgement_is_never_resubmitted_and_keeps_cost_reservation(monkeypatch):
    j, c, m, cell = fixture(monkeypatch)
    transport = Transport(fail=True)
    with pytest.raises(RuntimeError):
        execute(j, m, cell, c, transport)
    assert execute(j, m, cell, c, transport) == "outcome_unknown" and transport.calls == 1
    assert ledger(j, m) == (1, __import__("decimal").Decimal("0.50"))
    row = history(j, cell)[0]
    assert row["intent"] and row["receipt"] is None


def test_missing_credential_is_explicit_and_does_not_consume_an_attempt(monkeypatch):
    j, c, m, cell = fixture(monkeypatch)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    transport = Transport()
    assert execute(j, m, cell, c, transport) == "not_configured"
    assert transport.calls == 0 and ledger(j, m)[0] == 0
    assert j.get("unavailable:" + cell["cell_id"])["reason"] == "credential_missing"


def test_local_reprocess_keeps_failure_and_uses_archived_receipt_without_key_or_network(
    monkeypatch, tmp_path
):
    j, c, m, cell = fixture(monkeypatch)
    transport = Transport()
    real = results.normalize
    monkeypatch.setattr(
        results,
        "normalize",
        lambda *args: (_ for _ in ()).throw(ContractError("test_local_normalization_failure")),
    )
    assert execute(j, m, cell, c, transport) == "processing_failed"
    key = prefix(cell["cell_id"], 1)
    failure = deepcopy(j.get(key + ":failure"))
    receipt = deepcopy(j.get(key + ":receipt"))
    monkeypatch.setattr(results, "normalize", real)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    monkeypatch.setattr("podgen.runtime.evaluation.audio.reprocess.prepare", lambda *args: m)
    assert reprocess(j, c, cell["cell_id"], "Use fixed local normalizer") == "completed"
    assert transport.calls == 1 and j.get(key + ":failure") == failure and j.get(key + ":receipt") == receipt
    row = history(j, cell)[0]
    assert len(row["processing"]) == 2 and row["result"]["normalization"]["normalization_type"] == "linear"
    assert row["result"]["cost"]["provider_reported_headers"]["character-cost"] == "123"
    assert row["result"]["cost"]["status"] == "submitted_text_estimate"
    summary = export(j, tmp_path / "export")
    assert (
        summary["listening_samples"] == 1
        and summary["expected_samples"] == 20
        and not summary["full_five_provider_comparison_complete"]
    )
    page = (tmp_path / "export/blind/index.html").read_text()
    assert "fixture-hash" in page and "Текст для второго прослушивания" in page
    assert "elevenlabs" not in page and "fixture-elevenlabs" not in page
    assert list((tmp_path / "export/private/sources").glob("*.json"))
    assert (tmp_path / "export/private" / cell["cell_id"] / "take-1/processing-2/blind.wav").exists()
    assert (tmp_path / "export/private" / cell["cell_id"] / "take-1/processing-1/processing.json").exists()


def test_completed_sample_resume_has_no_provider_call(monkeypatch):
    j, c, m, cell = fixture(monkeypatch)
    transport = Transport()
    assert execute(j, m, cell, c, transport) == "completed"
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert execute(j, m, cell, c, transport) == "completed" and transport.calls == 1
    assert "not-persisted" not in repr(j.records)


def test_paid_artistic_retake_does_not_replace_the_first_usable_blind_baseline(monkeypatch, tmp_path):
    import json

    j, c, m, cell = fixture(monkeypatch)
    transport = Transport()
    assert execute(j, m, cell, c, transport) == "completed"
    assert execute(j, m, cell, c, transport, retake_reason="Explicit artistic alternative") == "completed"
    result = export(j, tmp_path / "export")
    key = json.loads((tmp_path / "export/private/blind-key.json").read_text())
    assert key[0]["selected_take"] == 1 and key[0]["retake_count"] == 1
    assert transport.calls == 2 and result["attempts_submitted"] == 2
    assert (tmp_path / "export/private" / cell["cell_id"] / "take-2/prepared.json").exists()


def test_budget_exhaustion_cannot_send_a_second_attempt(monkeypatch):
    j, c, m, cell = fixture(monkeypatch)
    c = c.model_copy(update=dict(max_attempts=1))
    transport = Transport()
    assert execute(j, m, cell, c, transport) == "completed"
    assert execute(j, m, cell, c, transport, retake_reason="Another performance") == "budget_exhausted"
    assert transport.calls == 1 and ledger(j, m)[0] == 1
    assert j.get(prefix(cell["cell_id"], 2) + ":budget")["status"] == "budget_exhausted"


def test_provider_quota_error_is_archived_and_never_automatically_retried(monkeypatch):
    from podgen.audio.synthesis.contracts import HttpReceipt

    j, c, m, cell = fixture(monkeypatch)

    class Quota:
        def __init__(self):
            self.calls = 0

        def submit(self, *args):
            self.calls += 1
            return HttpReceipt(429, {"request-id": "refused"}, b'{"error":"quota"}', 10)

    transport = Quota()
    assert execute(j, m, cell, c, transport) == "provider_rejected"
    assert execute(j, m, cell, c, transport) == "provider_rejected" and transport.calls == 1
    row = history(j, cell)[0]
    assert row["receipt"]["status_code"] == 429 and row["failure"]["phase"] == "provider"


def test_successful_original_then_failed_reprocess_keeps_baseline_and_exports_error(monkeypatch, tmp_path):
    import json

    j, c, m, cell = fixture(monkeypatch)
    transport = Transport()
    assert execute(j, m, cell, c, transport) == "completed"
    original = deepcopy(history(j, cell)[0]["result"])
    monkeypatch.setattr(
        results, "normalize", lambda *args: (_ for _ in ()).throw(ContractError("new_processing_failed"))
    )
    monkeypatch.setattr("podgen.runtime.evaluation.audio.reprocess.prepare", lambda *args: m)
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert reprocess(j, c, cell["cell_id"], "Explicit later processing revision") == "processing_failed"
    summary = export(j, tmp_path / "export")
    assert summary["listening_samples"] == 1 and transport.calls == 1
    mapping = json.loads((tmp_path / "export/private/blind-key.json").read_text())
    assert mapping[0]["selected_take"] == 1
    expected = j.bytes(prefix(cell["cell_id"], 1) + ":result", original["blind_audio_blob"])
    assert next((tmp_path / "export/blind").glob("*.wav")).read_bytes() == expected
    revisions = history(j, cell)[0]["processing"]
    assert revisions[0]["result"] == original and revisions[1]["failure"]["status"] == "processing_failed"
