import html
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.audio.export_directory import bind_directory, write_json
from podgen.runtime.evaluation.audio.immutable_export import write_bytes
from podgen.runtime.evaluation.audio.immutable_export import write_json as immutable_json

from ..exchange_history import attempts
from .approval import verify_proposal
from .attempts import ledger, state
from .contracts import contextual, prefix
from .localization_export import export as export_localization
from .subject import resolve


def export_proposal(journal, proposal_id, folder):
    root, folder = prefix(proposal_id), Path(folder)
    proposal = verify_proposal(journal, proposal_id)
    bind_directory(
        folder,
        "proposal-binding.json",
        dict(
            contract="audio-local-proposal-export/1",
            proposal_ref=journal.ref(root + ":proposal").model_dump(mode="json"),
            proposal_hash=proposal["proposal_hash"],
        ),
        proposal,
    )
    payload = proposal["payload"]
    immutable_json(folder / "proposal.json", proposal)
    for field, filename in (
        ("context_audio_blob", "context.wav"),
        ("selected_audio_blob", "selected.wav"),
        ("wire_blob", "private/wire-request.json"),
        ("timeline_blob", "private/timeline.json"),
    ):
        write_bytes(folder / filename, journal.bytes(root + ":proposal", payload[field]))
    rows = attempts(journal, root)
    write_json(folder / "attempts.json", rows)
    for row in rows:
        if row["receipt"]:
            write_bytes(
                folder / "private" / f"attempt-{row['number']}-raw.bin",
                journal.bytes(row["key"] + ":receipt", row["receipt"]["raw_response_blob"]),
            )
        for revision in row["processing"]:
            if revision["result"]:
                write_bytes(
                    folder / "private" / (str(journal.ref(revision["key"] + ":result").record_id) + ".wav"),
                    journal.bytes(revision["key"] + ":result", revision["result"]["decoded_pcm_blob"]),
                )
    approved = journal.get(root + ":approval")
    count, cost = ledger(journal, root)
    status = dict(
        proposal_hash=proposal["proposal_hash"],
        approved=approved is not None,
        delivery_state=state(journal, rows[-1]["key"]) if rows else "not_submitted",
        requests_submitted=count,
        cost_or_reservation_usd=str(cost),
        production_ready=False,
    )
    if contextual(payload) and approved and rows and state(journal, rows[-1]["key"]) == "synthesized":
        status["localization"] = export_localization(journal, proposal_id, folder)
        status["delivery_state"] = status["localization"]["state"]
    write_json(folder / "status.json", status)
    region = payload["frame_range"]
    start, end = (
        region["start_frame"] / region["sample_rate_hz"],
        region["end_frame"] / region["sample_rate_hz"],
    )
    synthesis_preview = ""
    if contextual(payload):
        spoken = "\n".join(row["text"] for row in payload["synthesis_input"]["utterances"])
        synthesis_preview = (
            "<h2>Текст для озвучивания с соседними предложениями</h2>"
            f"<pre>{html.escape(spoken)}</pre>"
            "<p>Будет озвучен весь этот текст. В запись вставится только выбранный фрагмент выше.</p>"
        )
    page = (
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Замена фрагмента</title>'
        "<style>body{max-width:850px;margin:40px auto;font:18px system-ui}audio{width:100%}pre{white-space:pre-wrap}</style>"
        f"<h1>Предложенный фрагмент: {start:.3f}–{end:.3f} с</h1><pre>{html.escape(payload['text'])}</pre>"
        + synthesis_preview
        + '<p>С соседним контекстом</p><audio controls src="context.wav"></audio>'
        '<p>Точный диапазон замены</p><audio controls src="selected.wav"></audio>'
        f"<p>Одобрено: {'да' if approved else 'нет'}. До точного одобрения генерации нет.</p></html>"
    )
    write_json(folder / "approval.json", approved)
    (folder / "index.html").write_text(page)
    return status


def export_master(journal, ref, folder):
    subject = resolve(journal, RuntimeRef.model_validate(ref))
    result, folder = subject["result"], Path(folder)
    key = "local-master:" + result["version_id"] + ":result"
    bind_directory(
        folder,
        "master-binding.json",
        dict(contract="audio-local-master-export/1", master_ref=ref, result_hash=digest(result)),
        result,
    )
    immutable_json(folder / "result.json", result)
    write_bytes(folder / "master.wav", journal.bytes(key, result["master_pcm_blob"]))
    write_bytes(folder / "timeline.json", journal.bytes(key, result["timeline_blob"]))
    rows = []
    for i, audition in enumerate(result.get("auditions", []), 1):
        filename = f"replacement-{i}-context.wav"
        write_bytes(folder / filename, journal.bytes(key, audition["audio_blob"]))
        rows.append(f'<p>Замена {i} с соседней речью</p><audio controls src="{filename}"></audio>')
    diagnostic_rows = []
    for i, join in enumerate(result["joins"], 1):
        filename = f"join-{i}-{join['side']}.wav"
        write_bytes(folder / filename, journal.bytes(key, join["audio_blob"]))
        diagnostic_rows.append(f'<p>Стык {i}: {join["side"]}</p><audio controls src="{filename}"></audio>')
    (folder / "index.html").write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Новая версия</title>'
        "<style>body{max-width:850px;margin:40px auto;font:18px system-ui}audio{width:100%}</style>"
        '<h1>Новая версия записи</h1><audio controls src="master.wav"></audio>'
        + "".join(rows)
        + "<details><summary>Диагностика отдельных стыков</summary>"
        + "".join(diagnostic_rows)
        + "</details></html>"
    )
    return dict(
        master_ref=ref,
        version_id=result["version_id"],
        duration_seconds=result["metrics"]["duration_seconds"],
        listening_status=result["listening_status"],
        production_ready=False,
    )
