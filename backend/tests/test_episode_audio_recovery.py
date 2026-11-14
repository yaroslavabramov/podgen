"""A paid synthesis receipt survives a local policy correction without a second POST."""

from copy import deepcopy

from podgen.audio.media.processing import VERSION
from podgen.episodes.production.audio import FullRenderConfig
from podgen.runtime.evaluation.audio.attempts import execute, history, ledger, prefix
from podgen.runtime.evaluation.audio.results import ASSEMBLY_INPUT
from tests.audio_cases import response, signal
from tests.test_audio_evaluation import fixture


def test_assembly_input_reprocesses_saved_high_crest_pcm_and_preserves_failed_revision(monkeypatch):
    journal, comparison, manifest, cell = fixture(monkeypatch)
    source = signal(extremes=True)

    class Speech:
        calls = 0

        def submit(self, request, credential):
            self.calls += 1
            return response(request.provider, source)

    speech = Speech()
    assert execute(journal, manifest, cell, comparison, speech) == "processing_failed"
    key = prefix(cell["cell_id"], 1)
    failure = deepcopy(journal.get(key + ":failure"))
    assert failure["reason"] == "audio_linear_normalization_exceeds_true_peak"
    receipt, decoded = deepcopy(journal.get(key + ":receipt")), deepcopy(journal.get(key + ":decoded"))
    original_charge = ledger(journal, manifest)
    assert journal.get(key + ":processing")["processing_contract"] == VERSION
    raw = comparison.model_dump(mode="json")
    render = FullRenderConfig.model_validate(
        dict(raw, contract="episode-audio-render/1", expected_voices_per_provider=comparison.voice_counts)
    )
    assert render.processing_contract == ASSEMBLY_INPUT
    # Existing saved manifests/requests retain their original identity. Only
    # the local processing revision changes; neither credentials nor HTTP are needed.
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")

    class Forbidden:
        def submit(self, *args):
            raise AssertionError("saved receipt must not be submitted again")

    assert execute(journal, manifest, cell, render, Forbidden()) == "completed"
    first = history(journal, cell)[0]
    assert len(first["processing"]) == 2 and first["processing"][0]["failure"] == failure
    revision = first["processing"][-1]
    assert revision["prepared"]["processing_contract"] == ASSEMBLY_INPUT
    assert journal.get(key + ":failure") == failure
    assert journal.get(key + ":receipt") == receipt and journal.get(key + ":decoded") == decoded
    assert revision["result"]["decoded_pcm_blob"] == decoded["pcm_blob"]
    assert revision["result"]["blind_audio_blob"] == decoded["pcm_blob"]
    assert revision["result"]["normalization"]["normalization_type"] == "none"
    assert revision["result"]["normalization"]["applied_gain_db"] == 0
    assert speech.calls == 1
    assert ledger(journal, manifest) == original_charge
    assert execute(journal, manifest, cell, render, Forbidden()) == "completed"
    assert len(history(journal, cell)[0]["processing"]) == 2
