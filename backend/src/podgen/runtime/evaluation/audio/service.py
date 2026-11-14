from .attempts import OperationLedger, baseline, execute_sample
from .matrix import full_comparison_complete
from .prepare import prepare


def run(journal, config, transport, *, progress=lambda value: None, retake_cell=None, reason=None):
    with journal.lock():
        manifest = prepare(journal, config)
        if retake_cell is not None and (
            not reason or retake_cell not in {c["cell_id"] for c in manifest["cells"]}
        ):
            raise ValueError("explicit_audio_retake_cell_and_reason_required")
        statuses = []
        operation = OperationLedger(journal, manifest)
        for cell in manifest["cells"]:
            if retake_cell is not None and cell["cell_id"] != retake_cell:
                continue
            result = execute_sample(
                journal,
                manifest,
                cell,
                config,
                transport,
                retake_reason=reason if retake_cell else None,
                operation_ledger=operation,
            )
            value = dict(
                cell_id=cell["cell_id"],
                profile_id=cell["profile_id"],
                fragment_id=cell["fragment_id"],
                status=result.status,
            )
            statuses.append(value)
            progress(value)
        count, cost = operation.totals(journal, manifest)
        selected = [baseline(operation.rows[c["cell_id"]]) for c in manifest["cells"]]
        completed = sum(bool(row and row["result"]) for row in selected)
        result = dict(
            contract="audio-comparison-status/1",
            expected_samples=manifest["expected_samples"],
            prepared_samples=len(manifest["cells"]),
            completed_samples=completed,
            requested_matrix_complete=completed == manifest["expected_samples"],
            full_five_provider_comparison_complete=full_comparison_complete(manifest, completed),
            attempts_submitted=count,
            cost_or_reservation_usd=str(cost),
            samples=statuses,
            production_ready=False,
        )
        journal.base.record(result)
        return result
