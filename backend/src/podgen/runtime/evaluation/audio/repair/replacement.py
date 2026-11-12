from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..assembly.contracts import AssemblyConfigV2
from ..assembly.contracts import prefix as assembly_prefix
from ..assembly.service import build
from ..subject_contracts import SynthesisPart
from .contracts import prefix
from .parent import parts
from .subject import resolve


def validate_replacements(journal, config):
    parent, old = parts(journal, config.parent_subject)
    if len(old) != len(config.bindings):
        raise ContractError("replacement_must_preserve_original_fragment_boundaries")
    for before, after in zip(old, config.bindings, strict=True):
        if before == after:
            continue
        if after.kind != "repair_part":
            raise ContractError("replacement_requires_confirmed_repair_part")
        part = resolve(journal, after.ref)
        if part["result"]["parent_subject"] != config.parent_subject.model_dump(mode="json") or part[
            "result"
        ]["parent_part"] != before.model_dump(mode="json"):
            raise ContractError("replacement_repair_is_for_different_parent_or_fragment")


def build_replacement(journal, config, repair_result_ref):
    _, bindings = parts(journal, config.parent_subject)
    replacement = [
        SynthesisPart(kind="repair_part", ref=repair_result_ref) if p == config.part_source else p
        for p in bindings
    ]
    if config.parent_subject.kind == "assembly":
        result = journal.base.read(config.parent_subject.ref)
        manifest = journal.base.read(RuntimeRef.model_validate(result["manifest_ref"]))
        parent = manifest["configuration"]
        settings = {
            k: parent[k] for k in ("preferred_lufs", "true_peak_dbtp", "headroom_db", "join_context_seconds")
        }
    else:
        parent = journal.get("manifest")["configuration"]
        settings = dict(
            preferred_lufs=parent["loudness_lufs"],
            true_peak_dbtp=parent["true_peak_dbtp"],
            headroom_db=0.3,
            join_context_seconds=10,
        )
    assembly = AssemblyConfigV2(
        assembly_id=uuid5(config.repair_id, "assembly:" + str(repair_result_ref.record_id)),
        comparison_run_id=config.comparison_run_id,
        part_sources=replacement,
        parent_subject=config.parent_subject,
        **settings,
    )
    ref = build(journal, assembly)
    key = prefix(config.repair_id) + ":replacement:" + str(repair_result_ref.record_id)
    journal.put(
        key,
        dict(
            contract="fragment-repair-reassembly/1",
            repair_result_ref=repair_result_ref.model_dump(mode="json"),
            assembly_configuration=assembly.model_dump(mode="json"),
            assembly_ref=ref,
            source_ref=journal.ref(assembly_prefix(assembly.assembly_id) + ":source").model_dump(mode="json"),
            verification_status="requires_new_full_master_asr_audio_review_and_join_review",
            production_ready=False,
        ),
    )
    return journal.get(key)
