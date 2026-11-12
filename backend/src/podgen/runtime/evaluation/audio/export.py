"""Export anonymous listening material separately from the complete evaluator provenance."""

import html
import json
import random
from pathlib import Path

from .attempts import baseline, history, ledger
from .matrix import full_comparison_complete


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def export(journal, folder):
    manifest = journal.get("manifest")
    if manifest is None:
        raise ValueError("audio_comparison_not_prepared")
    folder = Path(folder)
    private = folder / "private"
    private.mkdir(parents=True, exist_ok=True)
    blind = folder / "blind"
    blind.mkdir(parents=True, exist_ok=True)
    write_json(private / "manifest.json", manifest)
    for ref in manifest["source_refs"].values():
        from podgen.domain.contracts.common import RuntimeRef

        source = journal.base.read(RuntimeRef.model_validate(ref))
        write_json(private / "sources" / (str(RuntimeRef.model_validate(ref).record_id) + ".json"), source)
    cells = list(manifest["cells"])
    random.Random(manifest["configuration"]["blind_seed"]).shuffle(cells)
    mapping, items, statuses = [], [], []
    for n, cell in enumerate(cells, 1):
        label = f"S{n:03d}"
        rows = history(journal, cell)
        write_json(private / cell["cell_id"] / "attempts.json", rows)
        for row in rows:
            place = private / cell["cell_id"] / f"take-{row['take']}"
            place.mkdir(parents=True, exist_ok=True)
            write_json(place / "prepared.json", row["prepared"])
            (place / "wire-request.json").write_bytes(
                journal.bytes(row["key"] + ":prepared", row["prepared"]["wire_request_blob"])
            )
            if row["receipt"]:
                write_json(place / "receipt.json", row["receipt"])
                (place / "raw-http-response.bin").write_bytes(
                    journal.bytes(row["key"] + ":receipt", row["receipt"]["raw_response_blob"])
                )
            audio = journal.get(row["key"] + ":audio")
            if audio:
                write_json(place / "audio.json", audio)
                (place / "provider-audio.bin").write_bytes(
                    journal.bytes(row["key"] + ":audio", audio["provider_audio_blob"])
                )
            for revision_number, revision in enumerate(row["processing"], 1):
                where = place / f"processing-{revision_number}"
                write_json(where / "processing.json", revision)
                for key, name, field in (
                    ("decoded", "decoded.wav", "pcm_blob"),
                    ("result", "blind.wav", "blind_audio_blob"),
                ):
                    value = journal.get(revision["key"] + ":" + key)
                    if value:
                        write_json(where / (key + ".json"), value)
                        (where / name).write_bytes(journal.bytes(revision["key"] + ":" + key, value[field]))
        selected = baseline(rows)
        latest = rows[-1] if rows else None
        unavailable = journal.get("unavailable:" + cell["cell_id"])
        state = "prepared"
        if selected and selected["result"]:
            state = "completed"
        elif latest and latest["failure"]:
            state = latest["failure"]["status"]
        elif latest and latest["intent"] and not latest["receipt"]:
            state = "outcome_unknown"
        elif latest and journal.get(latest["key"] + ":budget"):
            state = "budget_exhausted"
        elif not rows and unavailable:
            state = "not_configured"
        statuses.append(
            dict(
                cell_id=cell["cell_id"],
                profile_id=cell["profile_id"],
                fragment_id=cell["fragment_id"],
                status=state,
                unavailable=unavailable,
                latest_processing_failure=selected["failure"] if selected else None,
            )
        )
        if selected and selected["result"]:
            filename = label + ".wav"
            (blind / filename).write_bytes(
                journal.bytes(selected["processing_key"] + ":result", selected["result"]["blind_audio_blob"])
            )
            source_record = journal.base.read(RuntimeRef.model_validate(cell["source_ref"]))
            transcript = "\n".join(u["text"] for u in source_record["source"]["utterances"])
            items.append(
                dict(sample=label, file=filename, fragment=cell["fragment_id"], transcript=transcript)
            )
            mapping.append(
                dict(
                    sample=label,
                    cell=cell,
                    selected_take=selected["take"],
                    selected_processing_key=selected["processing_key"],
                    retake_count=len(rows) - 1,
                )
            )
    current_files = {item["file"] for item in items}
    for obsolete in blind.glob("S[0-9][0-9][0-9].wav"):
        if obsolete.name not in current_files:
            obsolete.unlink()
    write_json(private / "blind-key.json", mapping)
    write_json(blind / "samples.json", items)
    cards = "".join(
        f'<article><h2>{html.escape(item["sample"])} · {html.escape(item["fragment"])}</h2><audio controls preload="none" src="{item["file"]}"></audio><details><summary>Текст для второго прослушивания</summary><p>{html.escape(item["transcript"])}</p></details><p><label>Заметки и время дефекта <input data-sample="{item["sample"]}" placeholder="00:12 — ударение"></label></p></article>'
        for item in items
    )
    page = (
        """<!doctype html><html lang="ru"><meta charset="utf-8"><title>Сравнение голосов</title><style>body{max-width:760px;margin:40px auto;font:18px system-ui;background:#faf9f6;color:#242424}article{padding:20px;border-bottom:1px solid #ddd}audio{width:100%}input{width:100%;padding:10px}</style><h1>Сравнение голосов</h1><p>Послушайте записи при одном положении громкости. Отметьте естественность, смысловые акценты и желание слушать дальше. Заметки сохраняются в этом браузере.</p>"""
        + cards
        + """<script>document.querySelectorAll('input').forEach(x=>{let k='podgen-audio-"""
        + manifest["configuration_hash"]
        + """-'+x.dataset.sample;x.value=localStorage.getItem(k)||'';x.oninput=()=>localStorage.setItem(k,x.value)})</script></html>"""
    )
    (blind / "index.html").write_text(page)
    count, cost = ledger(journal, manifest)
    summary = dict(
        contract="audio-comparison-export/1",
        prepared_samples=len(cells),
        expected_samples=manifest["expected_samples"],
        listening_samples=len(items),
        requested_matrix_complete=len(items) == manifest["expected_samples"],
        full_five_provider_comparison_complete=full_comparison_complete(manifest, len(items)),
        availability=manifest["availability"],
        samples=statuses,
        attempts_submitted=count,
        cost_or_reservation_usd=str(cost),
        purpose="evaluation",
        production_ready=False,
    )
    write_json(folder / "status.json", summary)
    return summary
