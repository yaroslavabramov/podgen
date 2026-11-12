"""Export the exact recognition input, wire, receipts and local extraction history."""

from ..exchange_history import attempts
from ..export_directory import write_json
from ..immutable_export import write_bytes
from ..immutable_export import write_json as immutable_json
from .localization import binding, current
from .localization_state import proposal_ledger


def export(journal, proposal_id, folder):
    _, _, _, root = binding(journal, proposal_id)
    manifest = journal.get(root + ":manifest")
    folder = folder / "private" / root.rsplit(":", 1)[-1]
    if manifest:
        immutable_json(folder / "manifest.json", manifest)
        for field, name in (("transport_audio_blob", "asr-input.flac"), ("wire_blob", "asr-wire.bin")):
            write_bytes(folder / name, journal.bytes(root + ":manifest", manifest[field]))
    rows = attempts(journal, root)
    write_json(folder / "attempts.json", rows)
    for row in rows:
        if row["receipt"]:
            write_bytes(
                folder / f"asr-{row['number']}-response.bin",
                journal.bytes(row["key"] + ":receipt", row["receipt"]["raw_response_blob"]),
            )
    number = 1
    while journal.get(f"{root}:extraction:{number}") is not None:
        key = f"{root}:extraction:{number}"
        for suffix in ("", ":recognition", ":failure"):
            value = journal.get(key + suffix)
            if value is not None:
                immutable_json(folder / f"extraction-{number}{suffix.replace(':', '-')}.json", value)
        number += 1
    result = journal.get(root + ":result")
    if result:
        immutable_json(folder / "extraction.json", result)
        write_bytes(folder / "middle.wav", journal.bytes(root + ":result", result["extracted_audio_blob"]))
    count, cost = proposal_ledger(journal, proposal_id)
    return dict(
        state=current(journal, proposal_id),
        requests_submitted=count,
        cost_or_reservation_usd=str(cost),
        failure=journal.get(root + ":failure") if not result else None,
    )
