from ..export_directory import write_json
from ..immutable_export import write_bytes
from ..immutable_export import write_json as immutable_json
from .deliveries import latest
from .processing import revisions


def export_results(journal, config, folder):
    rows = revisions(journal, config.audit_id)
    successful = []
    for row in rows:
        if not row["result"]:
            continue
        ref = journal.ref(row["key"] + ":result").model_dump(mode="json")
        place = folder / "results" / ref["record_id"]
        path = place / "candidates.json"
        immutable_json(path, row["result"])
        for candidate in row["result"]["report"]["candidates"]:
            if candidate.get("clip_blob"):
                write_bytes(
                    place / (candidate["candidate_id"] + ".wav"),
                    journal.bytes(row["key"] + ":result", candidate["clip_blob"]),
                )
        successful.append(dict(result_ref=ref, path=f"results/{ref['record_id']}/candidates.json"))
    current = rows[-1] if rows else None
    receipt_ref = journal.ref(latest(journal, config)["key"] + ":receipt").model_dump(mode="json")
    active = current if current and current["prepared"]["source_receipt_ref"] == receipt_ref else None
    selected = successful[-1] if active and active["result"] else None
    pointer = dict(
        contract="audio-fidelity-current/2",
        current_processing_ref=journal.ref(active["key"]).model_dump(mode="json") if active else None,
        current_result=selected,
        last_successful_result=successful[-1] if successful else None,
    )
    # This owned projection explicitly clears the old report on failed processing.
    write_json(folder / "current.json", pointer)
    write_json(
        folder / "candidates.json", dict(active["result"] if selected else dict(report=None), **pointer)
    )
    return pointer, active
