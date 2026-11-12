from decimal import Decimal

from podgen.audio.synthesis.contracts import PreparedRequest
from podgen.audio.synthesis.transport import encode_request
from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError

from ..fidelity.snapshots import schema_blob
from ..subjects import synthesis_part
from .contracts import RepairConfig, prefix
from .evidence import verify


def prepare(journal, config):
    if journal.run_id != config.comparison_run_id:
        raise ContractError("repair_wrong_comparison_run")
    key = prefix(config.repair_id)
    saved = journal.get(key + ":manifest")
    raw = config.model_dump(mode="json")
    if saved:
        if saved["configuration_hash"] != digest(raw):
            raise Conflict("repair_configuration_changed")
        return saved
    part = synthesis_part(journal, config.part_source)
    evidence = verify(journal, config, part)
    if Decimal(config.reservation_per_request_usd) < Decimal(part["profile"]["reservation_usd"]):
        raise ContractError("repair_reservation_below_frozen_request_envelope")
    depth = part["repair_depth"] + 1
    if depth > config.max_quality_depth:
        raise ContractError("repair_quality_depth_exhausted_needs_attention")
    original = journal.bytes(part["wire_authority_key"], part["original_wire_blob"])
    if encode_request(PreparedRequest.model_validate(part["original_request"])) != original:
        raise ContractError("repair_original_wire_does_not_match_frozen_request")
    manifest = dict(
        contract="confirmed-fragment-repair-manifest/1",
        configuration=raw,
        configuration_hash=digest(raw),
        parent_subject=config.parent_subject.model_dump(mode="json"),
        parent_part=config.part_source.model_dump(mode="json"),
        source_ref=part["source_ref"],
        source_script_ref=part["source"]["source_script_ref"],
        source_script_hash=part["source"]["source_script_hash"],
        profile=part["profile"],
        original_request=part["original_request"],
        wire_blob=journal.blob(original, "application/json"),
        origin_part_ref=part["origin_part_ref"],
        repair_depth=depth,
        evidence=evidence,
        config_schema_blob=schema_blob(journal, RepairConfig),
        quality_verification="required_after_reassembly",
        production_ready=False,
    )
    journal.put(key + ":manifest", manifest)
    return manifest
