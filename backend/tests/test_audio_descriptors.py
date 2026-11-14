"""Fault injection at durable audio boundaries; local media helpers are stubbed, no FFmpeg/SQL/API."""

from copy import deepcopy

import pytest

from podgen.domain.errors import ContractError
from podgen.episodes.production.audio import FullRenderConfig
from podgen.runtime.evaluation.audio import results
from podgen.runtime.evaluation.audio.attempts import OperationLedger, execute_sample, history, prefix
from tests.test_audio_evaluation import Transport, fixture


def prepared(monkeypatch):
    journal, config, manifest, cell = fixture(monkeypatch)
    monkeypatch.setattr(results, "tools", lambda: dict(ffmpeg="fixture", ffprobe="fixture"))
    monkeypatch.setattr(results, "decode", lambda audio: (b"original-pcm", dict(full_decode=True)))
    monkeypatch.setattr(
        results, "normalize", lambda *args: (b"listening-pcm", dict(normalization_type="linear"))
    )
    return journal, config, manifest, cell


def test_success_transfers_result_and_charge_without_rereading_receipt_or_history(monkeypatch):
    journal, config, manifest, cell = prepared(monkeypatch)
    operation = OperationLedger(journal, manifest)
    original = journal.bytes
    monkeypatch.setattr(journal, "bytes", lambda *args: pytest.fail("fresh HTTP reply was already received"))
    transport = Transport()
    result = execute_sample(journal, manifest, cell, config, transport, operation_ledger=operation)
    assert result.status == "completed" and result.attempts_submitted == 1
    assert result.result_ref == journal.ref(prefix(cell["cell_id"], 1) + ":result").model_dump(mode="json")
    assert result.baseline["result"]["cost"]["usd"] == str(result.committed_usd)
    monkeypatch.setattr(
        journal, "attempt_keys", lambda *args: pytest.fail("same operation rediscovered known history")
    )
    assert execute_sample(journal, manifest, cell, config, transport, operation_ledger=operation) == result
    assert transport.calls == 1
    monkeypatch.setattr(journal, "bytes", original)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize(
    "suffix,expected,calls",
    [(":submit", "outcome_unknown", 0), (":receipt", "completed", 1), (":result", "completed", 1)],
)
def test_lost_publication_ack_reusing_same_ledger_reloads_durable_cell(
    monkeypatch, suffix, expected, calls, error_type
):
    journal, config, manifest, cell = prepared(monkeypatch)
    operation = OperationLedger(journal, manifest)
    transport = Transport()
    put = journal.put
    interrupted = False

    def lost(key, value):
        nonlocal interrupted
        reference = put(key, value)
        if key.endswith(suffix) and not interrupted:
            interrupted = True
            raise error_type("committed but acknowledgement lost")
        return reference

    monkeypatch.setattr(journal, "put", lost)
    with pytest.raises(error_type, match="acknowledgement"):
        execute_sample(journal, manifest, cell, config, transport, operation_ledger=operation)
    key = prefix(cell["cell_id"], 1)
    intent = deepcopy(journal.get(key + ":submit"))
    assert operation.is_dirty(cell)
    result = execute_sample(journal, manifest, cell, config, transport, operation_ledger=operation)
    assert result.status == expected and transport.calls == calls
    assert not operation.is_dirty(cell)
    assert operation.totals(journal, manifest)[0] == 1
    assert journal.get(key + ":submit") == intent
    assert execute_sample(journal, manifest, cell, config, transport, operation_ledger=operation) == result
    assert transport.calls == calls


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
@pytest.mark.parametrize("suffix", [":processing", ":decoded", ":result"])
@pytest.mark.parametrize("restart", [False, True])
def test_interrupted_reprocess_resumes_exact_revision_without_resubmitting(
    monkeypatch, error_type, suffix, restart
):
    journal, comparison, manifest, cell = prepared(monkeypatch)
    monkeypatch.setattr(
        results,
        "decode",
        lambda audio: (
            b"original-pcm",
            dict(full_decode=True, integrated_lufs=-21, true_peak_dbtp=-2, sample_rate_hz=24000, channels=1),
        ),
    )

    def fail_normalization(*args):
        raise ContractError("original_normalization_failed")

    monkeypatch.setattr(results, "normalize", fail_normalization)
    transport = Transport()
    assert execute_sample(journal, manifest, cell, comparison, transport).status == "processing_failed"
    key = prefix(cell["cell_id"], 1)
    original = {name: journal.get(key + name) for name in (":processing", ":failure", ":receipt")}
    render = FullRenderConfig.model_validate(
        dict(
            comparison.model_dump(mode="json"),
            contract="episode-audio-render/1",
            expected_voices_per_provider=comparison.voice_counts,
        )
    )
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    operation = OperationLedger(journal, manifest)
    output_key = key + ":reprocess:1"
    put = journal.put
    interrupted = False

    def lost(record_key, value):
        nonlocal interrupted
        reference = put(record_key, value)
        if record_key == output_key + suffix and not interrupted:
            interrupted = True
            raise error_type("reprocess committed but acknowledgement lost")
        return reference

    monkeypatch.setattr(journal, "put", lost)
    with pytest.raises(error_type, match="acknowledgement"):
        execute_sample(journal, manifest, cell, render, transport, operation_ledger=operation)
    assert operation.is_dirty(cell)
    if restart:
        operation = OperationLedger(journal, manifest)
    result = execute_sample(journal, manifest, cell, render, transport, operation_ledger=operation)
    assert result.status == "completed" and result.attempts_submitted == 1
    assert result.result_ref == journal.ref(output_key + ":result").model_dump(mode="json")
    assert transport.calls == 1
    assert {name: journal.get(key + name) for name in original} == original
    revisions = history(journal, cell)[0]["processing"]
    assert [row["key"] for row in revisions] == [key, output_key]
    assert revisions[-1]["result"]["decoded_ref"] == journal.ref(output_key + ":decoded").model_dump(
        mode="json"
    )
    assert execute_sample(journal, manifest, cell, render, transport, operation_ledger=operation) == result
    assert transport.calls == 1
