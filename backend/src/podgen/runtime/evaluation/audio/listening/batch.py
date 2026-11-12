import math
from uuid import UUID

from pydantic import Field

from podgen.audio.media.processing import normalize, playback_input, tools
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import Contract
from podgen.domain.errors import Conflict, ContractError

from ..attempts import history
from ..prepare import prepare
from .selection import select


class ListeningConfig(Contract):
    batch_id: UUID
    preferred_lufs: float = Field(default=-20, ge=-40, le=-14)
    true_peak_dbtp: float = Field(default=-1, ge=-6, le=-1)
    headroom_db: float = Field(default=0.3, ge=0.1, le=3)
    blind_seed: int = 42
    repair_observed_fish_header: bool = False


def batch_key(batch_id):
    return "listening:" + str(batch_id)


def build(journal, comparison, config):
    """No transport or credentials: freeze measured playback inputs, then apply one shared gain target."""
    with journal.lock():
        original = prepare(journal, comparison)
        key = batch_key(config.batch_id)
        settings = config.model_dump(mode="json")
        definition = dict(settings=settings, comparison_configuration_hash=original["configuration_hash"])
        previous = journal.get(key + ":definition")
        if previous is not None and previous != definition:
            raise Conflict("listening_batch_configuration_changed")
        journal.put(key + ":definition", definition)
        saved = journal.get(key + ":manifest")
        if saved and saved["tool_versions"] != tools():
            raise Conflict("listening_batch_tool_versions_changed_create_new_batch")
        if saved and saved["settings"] != settings:
            raise Conflict("listening_batch_configuration_changed")
        if not saved:
            samples, excluded = [], []
            for cell in original["cells"]:
                decoded_key, decoded, errors = select(journal, cell, key, config.repair_observed_fish_header)
                if not decoded:
                    excluded.append(
                        dict(cell=cell, previous_processing_errors=errors, reason="no_usable_decoded_audio")
                    )
                    continue
                input_key = key + ":input:" + cell["cell_id"]
                value = journal.get(input_key)
                if value is None:
                    pcm, metrics = playback_input(journal.bytes(decoded_key, decoded["pcm_blob"]))
                    value = dict(
                        contract="audio-listening-playback-input/1",
                        pcm_blob=journal.blob(pcm, "audio/wav"),
                        metrics=metrics,
                        decoded_ref=journal.ref(decoded_key).model_dump(mode="json"),
                        decoded_key=decoded_key,
                        cell=cell,
                        previous_processing_errors=errors,
                        sample_rate_hz=48000,
                        channels=1,
                    )
                    journal.put(input_key, value)
                samples.append(
                    dict(key=input_key, ref=journal.ref(input_key).model_dump(mode="json"), value=value)
                )
            if not samples:
                raise ContractError("listening_batch_has_no_decoded_audio")
            ceiling = min(
                float(s["value"]["metrics"]["input_i"])
                + config.true_peak_dbtp
                - float(s["value"]["metrics"]["input_tp"])
                - config.headroom_db
                for s in samples
            )
            target = math.floor(min(config.preferred_lufs, ceiling) * 10) / 10
            receipts = [
                row["receipt"]
                for cell in original["cells"]
                for row in history(journal, cell)
                if row["receipt"]
            ]
            coverage = dict(
                configured_samples=len(original["cells"]),
                expected_samples=original["expected_samples"],
                complete_http_200=sum(
                    r["status_code"] == 200 and r["complete"] and not r["error"] for r in receipts
                ),
                provider_refusals=sum(not 200 <= r["status_code"] < 300 for r in receipts),
                listening_samples=len(samples),
            )
            saved = dict(
                contract="audio-listening-batch/1",
                coverage=coverage,
                settings=settings,
                comparison_manifest_ref=journal.ref("manifest").model_dump(mode="json"),
                comparison_configuration_hash=original["configuration_hash"],
                tool_versions=tools(),
                target_lufs=target,
                samples=samples,
                excluded=excluded,
                expected_samples=original["expected_samples"],
                availability=original["availability"],
                production_ready=False,
                speech_fidelity="pending_listening",
            )
            journal.put(key + ":manifest", saved)
        for sample in saved["samples"]:
            result_key = key + ":result:" + sample["value"]["cell"]["cell_id"]
            if journal.get(result_key):
                continue
            audio, report = normalize(
                journal.bytes(sample["key"], sample["value"]["pcm_blob"]),
                saved["target_lufs"],
                config.true_peak_dbtp,
            )
            journal.put(
                result_key,
                dict(
                    contract="audio-listening-result/1",
                    batch_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
                    playback_input_ref=sample["ref"],
                    blind_audio_blob=journal.blob(audio, "audio/wav"),
                    normalization=report,
                    production_ready=False,
                ),
            )
        return dict(
            batch_ref=journal.ref(key + ":manifest").model_dump(mode="json"), manifest_hash=digest(saved)
        )
