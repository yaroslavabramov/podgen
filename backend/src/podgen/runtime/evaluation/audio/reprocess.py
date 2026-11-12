"""An explicit local retry never obtains credentials or constructs a network transport."""

from podgen.audio.synthesis.contracts import HttpReceipt, PreparedRequest
from podgen.domain.errors import ContractError

from .attempts import history
from .prepare import prepare
from .processing_history import runs
from .results import process


def reprocess(journal, config, cell_id, reason):
    if not reason or not reason.strip():
        raise ContractError("audio_reprocess_reason_required")
    with journal.lock():
        manifest = prepare(journal, config)
        cell = next((c for c in manifest["cells"] if c["cell_id"] == cell_id), None)
        if cell is None:
            raise ContractError("audio_unknown_sample")
        attempts = history(journal, cell)
        if not attempts or not attempts[-1]["receipt"]:
            raise ContractError("audio_reprocess_requires_archived_receipt")
        current = attempts[-1]
        key = current["key"]
        receipt = current["receipt"]
        if not receipt["complete"] or not 200 <= receipt["status_code"] < 300:
            raise ContractError("audio_reprocess_requires_complete_success_response")
        revisions = runs(journal, key)
        output_key = f"{key}:reprocess:{len(revisions)}"
        profile = next(p for p in config.profiles if p.profile_id == cell["profile_id"])
        request = PreparedRequest.model_validate(cell["request"])
        response = HttpReceipt(
            receipt["status_code"],
            receipt["headers"],
            journal.bytes(key + ":receipt", receipt["raw_response_blob"]),
            receipt["elapsed_ms"],
        )
        journal.put(
            output_key + ":parent",
            dict(
                contract="audio-processing-parent/1",
                parent_ref=journal.ref(revisions[-1]["key"] + ":processing").model_dump(mode="json"),
            ),
        )
        return process(journal, key, profile, config, request, response, output_key=output_key, reason=reason)
