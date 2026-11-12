import html
import random
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError

from ..export import write_json
from .batch import batch_key


def export_batch(journal, batch_id, folder):
    key = batch_key(batch_id)
    manifest = journal.get(key + ":manifest")
    if manifest is None:
        raise ContractError("listening_batch_not_prepared")
    folder = Path(folder)
    binding = dict(
        batch_ref=journal.ref(key + ":manifest").model_dump(mode="json"), manifest_hash=digest(manifest)
    )
    binding_path = folder / "batch.json"
    if binding_path.exists():
        import json

        if json.loads(binding_path.read_text()) != binding:
            raise Conflict("listening_export_directory_bound_to_other_batch")
    write_json(binding_path, binding)
    write_json(folder / "private" / "manifest.json", manifest)
    samples = list(manifest["samples"])
    random.Random(manifest["settings"]["blind_seed"]).shuffle(samples)
    items, mapping, measurements, repairs = [], [], [], []
    for n, sample in enumerate(samples, 1):
        cell = sample["value"]["cell"]
        result_key = key + ":result:" + cell["cell_id"]
        result = journal.get(result_key)
        if result is None:
            raise ContractError("listening_batch_render_incomplete")
        label = f"S{n:03d}"
        filename = label + ".wav"
        blind = folder / "blind"
        blind.mkdir(parents=True, exist_ok=True)
        (blind / filename).write_bytes(journal.bytes(result_key, result["blind_audio_blob"]))
        source = journal.base.read(RuntimeRef.model_validate(cell["source_ref"]))
        items.append(
            dict(
                sample=label,
                file=filename,
                fragment=cell["fragment_id"],
                transcript="\n".join(u["text"] for u in source["source"]["utterances"]),
            )
        )
        decoded = journal.get(sample["value"]["decoded_key"])
        write_json(folder / "private" / label / "decoded.json", decoded)
        measurements.append(result["normalization"])
        if decoded.get("repair"):
            repairs.append(decoded["repair"])
            (folder / "private" / label / "repaired-container.wav").write_bytes(
                journal.bytes(sample["value"]["decoded_key"], decoded["repaired_container_blob"])
            )
        write_json(folder / "private" / label / "result.json", result)
        mapping.append(dict(sample=label, input=sample))
    write_json(folder / "private" / "blind-key.json", mapping)
    write_json(folder / "blind" / "samples.json", items)
    cards = "".join(
        f'<article><h2>Запись {item["sample"]}</h2><audio controls preload="none" src="{item["file"]}"></audio><details><summary>Текст для второго прослушивания</summary><p>{html.escape(item["transcript"])}</p></details><textarea data-sample="{item["sample"]}" placeholder="Заметки и время дефекта"></textarea></article>'
        for item in items
    )
    page = (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Прослушивание</title><style>body{max-width:760px;margin:40px auto;font:18px system-ui}article{padding:20px;border-bottom:1px solid #ddd}audio,textarea{width:100%}</style><h1>Сравнение голосов</h1><p>Слушайте при одном положении громкости. Оцените естественность, смысловые акценты и желание слушать дальше. Заметки сохраняются в браузере.</p>'
        + cards
    )
    page += (
        '<script>document.querySelectorAll("textarea").forEach(x=>{let k="podgen-listening-'
        + binding["manifest_hash"]
        + '-"+x.dataset.sample;x.value=localStorage.getItem(k)||"";x.oninput=()=>localStorage.setItem(k,x.value)})</script></html>'
    )
    (folder / "blind" / "index.html").write_text(page)
    status = dict(
        contract="audio-listening-export/1",
        **binding,
        listening_samples=len(items),
        expected_samples=manifest["expected_samples"],
        excluded_samples=len(manifest["excluded"]),
        requested_matrix_complete=len(items) == manifest["expected_samples"],
        availability=manifest["availability"],
        coverage=manifest["coverage"],
        target_lufs=manifest["target_lufs"],
        true_peak_dbtp=manifest["settings"]["true_peak_dbtp"],
        measured_playback=dict(
            min_integrated_lufs=min(m["integrated_lufs"] for m in measurements),
            max_integrated_lufs=max(m["integrated_lufs"] for m in measurements),
            max_true_peak_dbtp=max(m["true_peak_dbtp"] for m in measurements),
            all_same_target=all(m["target_lufs"] == manifest["target_lufs"] for m in measurements),
            container_repairs=len(repairs),
            all_repaired_pcm_payloads_unchanged=all(r["payload_unchanged"] for r in repairs),
        ),
        previous_processing_errors_location="private/manifest.json",
        speech_fidelity="pending_listening",
        production_ready=False,
    )
    write_json(folder / "status.json", status)
    return status
