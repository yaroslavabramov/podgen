from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from .contracts import prefix


def resolve(journal, ref):
    result = journal.base.read(ref)
    if result.get("contract") != "audio-evaluation-repair-part/1":
        raise ContractError("assembly_subject_is_not_repair_part")
    key = prefix(UUID(result["repair_id"]))
    manifest = journal.get(key + ":manifest")
    if not manifest or result["manifest_ref"] != journal.ref(key + ":manifest").model_dump(mode="json"):
        raise ContractError("repair_part_not_registered")
    found = any(
        r["result"] == result and journal.ref(r["key"] + ":result") == ref
        for row in attempts(journal, key)
        for r in row["processing"]
    )
    if not found or manifest["configuration"]["comparison_run_id"] != str(journal.run_id):
        raise ContractError("repair_part_outside_comparison")
    source = journal.base.read(RuntimeRef.model_validate(manifest["source_ref"]))
    decoded = journal.base.read(RuntimeRef.model_validate(result["decoded_ref"]))
    if (
        decoded["pcm_blob"] != result["decoded_pcm_blob"]
        or not decoded["metrics"]["full_decode"]
        or result["source_ref"] != manifest["source_ref"]
    ):
        raise ContractError("repair_part_source_or_pcm_mismatch")
    return dict(
        kind="repair_part",
        source=source,
        source_ref=manifest["source_ref"],
        result=result,
        audio_ref=ref.model_dump(mode="json"),
        audio_blob=result["decoded_pcm_blob"],
        decoded_ref=result["decoded_ref"],
        profile=manifest["profile"],
        original_request=manifest["original_request"],
        original_wire_blob=manifest["wire_blob"],
        wire_authority_key=key + ":manifest",
        repair_depth=result["repair_depth"],
        origin_part_ref=manifest["origin_part_ref"],
    )
