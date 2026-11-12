from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from ..export_directory import bind_directory, write_json
from .contracts import prefix


def export(journal, config, folder):
    key = prefix(config.assembly_id)
    manifest, result = journal.get(key + ":manifest"), journal.get(key + ":result")
    if not manifest or not result:
        raise ContractError("assembly_not_built")
    folder = Path(folder)
    binding = dict(
        contract="audio-assembly-export-binding/1",
        assembly_ref=journal.ref(key + ":result").model_dump(mode="json"),
        manifest_hash=digest(manifest),
    )
    bind_directory(folder, "assembly-binding.json", binding, manifest)
    write_json(folder / "private/manifest.json", manifest)
    write_json(folder / "private/result.json", result)
    write_json(folder / "frame-map.json", result["frame_map"])
    (folder / "private/assembly-pcm-before-gain.wav").write_bytes(
        journal.bytes(key + ":result", result["assembly_pcm_blob"])
    )
    (folder / "master.wav").write_bytes(journal.bytes(key + ":result", result["playback_audio_blob"]))
    rows = []
    for join in result["joins"]:
        filename = f"join-{join['join_index']}.wav"
        (folder / filename).write_bytes(journal.bytes(key + ":result", join["audio_blob"]))
        rows.append(
            f'<p>Стык {join["join_index"]}</p><audio controls preload="none" src="{filename}"></audio>'
        )
    (folder / "index.html").write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Прослушивание выпуска</title><style>body{max-width:760px;margin:40px auto;font:18px system-ui}audio{width:100%}</style><h1>Прослушивание выпуска</h1><p>Сохранность слов и звучание стыков ещё требуют проверки.</p><audio controls preload="none" src="master.wav"></audio>'
        + "".join(rows)
        + "</html>"
    )
    status = dict(
        binding,
        contract="audio-evaluation-assembly-export/1",
        source_script_hash=result["source_script_hash"],
        parts=len(result["frame_map"]),
        sample_frames=result["metrics"]["sample_frames"],
        sample_rate_hz=result["metrics"]["sample_rate_hz"],
        duration_seconds=result["metrics"]["duration_seconds"],
        coverage=result["coverage"],
        normalization=result["normalization"],
        acoustic_verdict="pending",
        production_ready=False,
    )
    write_json(folder / "status.json", status)
    return status
