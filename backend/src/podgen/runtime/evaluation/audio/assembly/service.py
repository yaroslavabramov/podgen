import math

from podgen.audio.assembly.maps import final_master_map
from podgen.audio.assembly.pcm import concatenate, read
from podgen.audio.media.processing import decode, normalize, playback_input, tools
from podgen.audio.synthesis.contracts import SynthesizedAudio
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import Conflict, ContractError

from .contracts import prefix
from .joins import build_joins
from .prepare import prepare


def build(journal, config):
    with journal.lock():
        manifest = prepare(journal, config)
        key = prefix(config.assembly_id)
        if journal.get(key + ":result"):
            return journal.ref(key + ":result").model_dump(mode="json")
        if manifest["tool_versions"] != tools():
            raise Conflict("assembly_tool_versions_changed_create_new_assembly")
        inputs = [
            journal.base.read_blob(ref, BlobRef.model_validate(p["audio_blob"]))
            for ref, p in zip(config.part_result_refs, manifest["parts"], strict=True)
        ]
        master, proof = concatenate(inputs)
        verified, metrics = decode(
            SynthesizedAudio(master, "audio/wav", proof["sample_rate_hz"], "wav", None, {}, "assembled_pcm")
        )
        if read(verified)[2] != read(master)[2] or metrics["sample_frames"] != proof["sample_frames"]:
            raise ContractError("assembly_full_decode_changed_pcm")
        _, measured = playback_input(master)
        ceiling = (
            float(measured["input_i"])
            + config.true_peak_dbtp
            - float(measured["input_tp"])
            - config.headroom_db
        )
        target = math.floor(min(config.preferred_lufs, ceiling) * 10) / 10
        playback, normalization = normalize(master, target, config.true_peak_dbtp)
        playback_params, playback_frames, _ = read(playback)
        rate = proof["sample_rate_hz"]
        playback_rate = playback_params[2]
        source_map = [
            dict(
                row,
                source_result_ref=p["result_ref"],
                source_ref=p["source_ref"],
                source_kind=p.get("source_kind", "comparison_result"),
            )
            for row, p in zip(proof["parts"], manifest["parts"], strict=True)
        ]
        mapping = final_master_map(source_map, rate, playback_rate, playback_frames)
        joins = build_joins(
            journal, config, mapping, proof["sample_frames"], rate, playback, playback_rate, playback_frames
        )
        _, final_metrics = decode(
            SynthesizedAudio(playback, "audio/wav", playback_rate, "wav", None, {}, "assembled_final_master")
        )
        if final_metrics["sample_frames"] != playback_frames:
            raise ContractError("assembly_final_master_decode_mismatch")
        final_blob = journal.blob(playback, "audio/wav")
        result = dict(
            contract="audio-evaluation-assembly-result/1",
            assembly_id=str(config.assembly_id),
            comparison_run_id=str(config.comparison_run_id),
            manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"),
            configuration_hash=manifest["configuration_hash"],
            source_ref=manifest["source_ref"],
            source_script_ref=manifest["source_script_ref"],
            source_script_hash=manifest["source_script_hash"],
            assembly_pcm_blob=journal.blob(master, "audio/wav"),
            master_pcm_blob=final_blob,
            playback_audio_blob=final_blob,
            pcm_assembly=proof,
            assembly_metrics=metrics,
            metrics=final_metrics,
            frame_map=mapping,
            joins=joins,
            normalization=dict(
                normalization,
                preferred_lufs=config.preferred_lufs,
                headroom_db=config.headroom_db,
                per_part_gain_applied=False,
            ),
            playback_timebase=dict(
                source_sample_rate_hz=rate,
                source_frames=proof["sample_frames"],
                playback_sample_rate_hz=playback_rate,
                playback_frames=playback_frames,
                conversion="uniform_sample_rate_conversion",
                boundary_rounding="nearest_frame",
            ),
            coverage=manifest["coverage"],
            acoustic_verdict="pending",
            production_ready=False,
        )
        journal.put(key + ":result", result)
        return journal.ref(key + ":result").model_dump(mode="json")
