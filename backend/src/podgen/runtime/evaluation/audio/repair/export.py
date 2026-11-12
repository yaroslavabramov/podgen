from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError

from ..assembly.contracts import load_assembly_config
from ..assembly.export import export as export_assembly
from ..exchange_history import attempts
from ..export_directory import bind_directory, read_exact, write_json
from .attempts import ledger, state
from .contracts import prefix
from .dispatch import delivery_stamp
from .dispatch import latest as latest_dispatch


def bind(journal, config, folder):
    root = prefix(config.repair_id)
    manifest = journal.get(root + ":manifest")
    if not manifest:
        raise ContractError("repair_not_prepared")
    bind_directory(
        folder,
        "repair-binding.json",
        dict(
            contract="fragment-repair-export-binding/1",
            manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"),
            manifest_hash=digest(manifest),
        ),
        manifest,
    )
    return manifest


def export(journal, config, folder):
    import json

    folder = Path(folder)
    manifest = bind(journal, config, folder)
    root = prefix(config.repair_id)
    write_json(folder / "private/manifest.json", manifest)
    write_json(
        folder / "private/source.json", journal.base.read(RuntimeRef.model_validate(manifest["source_ref"]))
    )
    (folder / "private/frozen-wire-request.json").write_bytes(
        journal.bytes(root + ":manifest", manifest["wire_blob"])
    )
    (folder / "private/config.schema.json").write_bytes(
        journal.bytes(root + ":manifest", manifest["config_schema_blob"])
    )
    rows = attempts(journal, root)
    for row in rows:
        place = folder / "private" / f"attempt-{row['number']}"
        write_json(place / "attempt.json", row)
        if row["receipt"]:
            (place / "raw-http-response.bin").write_bytes(
                journal.bytes(row["key"] + ":receipt", row["receipt"]["raw_response_blob"])
            )
        for i, revision in enumerate(row["processing"], 1):
            if not revision["result"]:
                continue
            where = place / f"processing-{i}"
            write_json(where / "result.json", revision["result"])
            (where / "part-pcm.wav").write_bytes(
                journal.bytes(revision["key"] + ":result", revision["result"]["decoded_pcm_blob"])
            )
            (where / "provider-audio.bin").write_bytes(
                journal.bytes(revision["key"] + ":result", revision["result"]["provider_audio_blob"])
            )
            outcome = journal.get(
                root + ":outcome:" + str(journal.ref(revision["key"] + ":result").record_id)
            )
            if outcome:
                result_id = str(journal.ref(revision["key"] + ":result").record_id)
                result_folder = folder / "results" / result_id
                archived = result_folder / "outcome.json"
                if archived.exists():
                    if not read_exact(archived, outcome):
                        raise Conflict("repair_outcome_export_changed")
                    continue
                export_assembly(
                    journal,
                    load_assembly_config(json.dumps(outcome["assembly_configuration"])),
                    result_folder / "replacement-master",
                )
                write_json(result_folder / "result.json", revision["result"])
                write_json(result_folder / "replacement-assembly.json", outcome["assembly_configuration"])
                write_json(result_folder / "verification.json", outcome["verification_configuration"])
                write_json(archived, outcome)
    active = rows[-1]["processing"][-1] if rows and rows[-1]["processing"] else None
    active_ref = journal.ref(active["key"] + ":result") if active and active["result"] else None
    active_outcome = journal.get(root + ":outcome:" + str(active_ref.record_id)) if active_ref else None
    current_pointer = dict(
        result_ref=active_ref.model_dump(mode="json") if active_ref else None,
        outcome_path=f"results/{active_ref.record_id}/outcome.json" if active_outcome else None,
        verification_config_path=f"results/{active_ref.record_id}/verification.json"
        if active_outcome
        else None,
        replacement_master_path=f"results/{active_ref.record_id}/replacement-master"
        if active_outcome
        else None,
    )
    write_json(folder / "current.json", current_pointer)
    current = state(journal, rows[-1]["key"]) if rows else "prepared"
    if current == "part_generated":
        current = "verification_required" if active_outcome else "reassembly_or_verification_required"
    count, cost = ledger(journal, root)
    dispatch = latest_dispatch(journal, config)
    active_dispatch = (
        dispatch
        if dispatch
        and dispatch["result_ref"] == current_pointer["result_ref"]
        and dispatch.get("delivery_stamp") == delivery_stamp(journal, config)
        else None
    )
    status = dict(
        contract="fragment-repair-export/1",
        status=current,
        requests_submitted=count,
        max_requests=config.max_requests,
        cost_or_reservation_usd=str(cost),
        repair_depth=manifest["repair_depth"],
        source_scope="whole_original_synthesis_fragment",
        current=current_pointer,
        last_delivery_state=state(journal, rows[-1]["key"]) if rows else "not_submitted",
        latest_operation=dispatch,
        operation_applies_to_current_result=bool(active_dispatch),
        current_stop_reason=active_dispatch["status"]
        if active_dispatch
        and active_dispatch["status"]
        in ("credential_missing", "budget_exhausted", "verification_preparation_failed")
        else None,
        source_script_hash=manifest["source_script_hash"],
        automatic_asr_or_acceptance=False,
        verification_status="required",
        production_ready=False,
    )
    write_json(folder / "status.json", status)
    return status
