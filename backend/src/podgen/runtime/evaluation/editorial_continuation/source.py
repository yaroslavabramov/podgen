"""Immutable source bindings and cumulative revision/budget admission."""

from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

from ..compiler import materials_hash
from ..contracts import WritingRunCommand, command_payload
from ..reading_outcome import read_outcome
from ..total_input_recovery import effective_ceiling

KEY = "editorial_continuation"
VERSION = "editorial-continuation/1"
MAX_REVISIONS = 3
STAGES = ("revise-1", "reader-review-1", "fact-review-1")


def continuation_id(parent):
    return uuid5(parent, VERSION)


def offset(manifest):
    binding = manifest.get(KEY)
    if binding is None:
        return 0
    if binding.get("contract") != VERSION:
        raise ContractError("unknown_editorial_continuation_contract")
    return binding["revisions_completed_before"]


def _source(journal, parent):
    manifest = journal.manifest(parent)
    command = WritingRunCommand.model_validate(manifest["command"])
    if (
        command.input_contract != "editorial-role-packets/1"
        or command.review_mode != "independent-readings/1"
    ):
        raise ContractError("editorial_continuation_requires_independent_readings")
    outcome = read_outcome(journal, parent)
    if outcome is None or outcome["status"] != "revision_limit_exhausted":
        raise ContractError("editorial_continuation_requires_exhausted_run")
    consumed = offset(manifest) + command.max_script_revisions
    if consumed >= MAX_REVISIONS:
        raise ContractError("editorial_continuation_revision_limit_exhausted")
    if command.max_script_revisions < 1:
        raise ContractError("editorial_continuation_revision_resources_missing")
    return manifest, command, outcome, consumed


def _command(source, parent, ceiling, additional_input, additional_output):
    values = source.model_dump(mode="json")
    values.update(
        run_id=str(continuation_id(parent)),
        editorial_parent_run_id=str(parent),
        editorial_additional_input_tokens=additional_input,
        editorial_additional_output_tokens=additional_output,
        max_script_revisions=1,
        initial_script_ref=None,
        planning_parent_run_id=None,
        planning_continuation_limit=None,
        limits=dict(
            source.limits.model_dump(mode="json"),
            max_total_input_tokens=ceiling + additional_input,
            max_total_output_tokens=source.limits.max_total_output_tokens + additional_output,
        ),
    )
    return WritingRunCommand.model_validate(values)


def build_command(journal, parent, *, additional_input, additional_output=0, run_id=None):
    if any(type(value) is not int or value < 0 for value in (additional_input, additional_output)):
        raise ContractError("editorial_continuation_invalid_allowance")
    if run_id is not None and run_id != continuation_id(parent):
        raise ContractError("editorial_continuation_run_id_mismatch")
    _, source, _, _ = _source(journal, parent)
    result = _command(source, parent, effective_ceiling(journal, parent), additional_input, additional_output)
    if journal.run(result.run_id) is not None and journal.manifest(result.run_id)[
        "command"
    ] != command_payload(result):
        raise ContractError("editorial_continuation_allowance_changed")
    return result


def source_binding(journal, command, materials):
    parent = command.editorial_parent_run_id
    manifest, source, outcome, consumed = _source(journal, parent)
    expected = _command(
        source,
        parent,
        effective_ceiling(journal, parent),
        command.editorial_additional_input_tokens,
        command.editorial_additional_output_tokens,
    )
    if command != expected or command.run_id != continuation_id(parent):
        raise ContractError("editorial_continuation_command_mismatch")
    if manifest["materials_hash"] != materials_hash(materials.as_payload()):
        raise ContractError("editorial_continuation_materials_changed")
    prior = manifest.get(KEY)
    plan = prior.get("accepted_plan") if prior else None
    if command.plan_mode == "own-plan" and plan is None:
        from ..planning import clearance_ref, verify_clearance

        proof = verify_clearance(journal, parent)
        plan = dict(
            clearance_ref=clearance_ref(parent).model_dump(mode="json"),
            candidate_ref=proof["candidate_plan_ref"],
            candidate_hash=proof["candidate_plan_hash"],
            review_ref=proof["plan_review_ref"],
        )
    if plan is not None:
        from ..planning import verify_clearance

        proof_record = journal.read(RuntimeRef.model_validate(plan["clearance_ref"]))
        proof = verify_clearance(journal, UUID(proof_record["run_id"]))
        if (proof["candidate_plan_ref"], proof["candidate_plan_hash"], proof["plan_review_ref"]) != (
            plan["candidate_ref"],
            plan["candidate_hash"],
            plan["review_ref"],
        ):
            raise ContractError("editorial_continuation_plan_changed")
    from ..reading_cycle import outcome_ref
    from .budget import inherited_usage

    usage, planning_usage = inherited_usage(journal, manifest, outcome)
    if (
        usage["input"] > command.limits.max_total_input_tokens
        or usage["output"] > command.limits.max_total_output_tokens
    ):
        raise ContractError("editorial_continuation_budget_insufficient")

    binding = dict(
        contract=VERSION,
        parent_run_id=str(parent),
        root_run_id=prior["root_run_id"] if prior else str(parent),
        parent_manifest_hash=digest(manifest),
        outcome_ref=outcome_ref(parent).model_dump(mode="json"),
        outcome_hash=digest(outcome),
        script_ref=outcome["script_ref"],
        script_hash=outcome["script_hash"],
        reading_refs=outcome["reading_refs"],
        accepted_plan=plan,
        materials_hash=manifest["materials_hash"],
        revisions_completed_before=consumed,
        max_total_revisions=MAX_REVISIONS,
        inherited_usage=usage,
        planning_ancestry_usage=planning_usage,
        parent_input_ceiling=effective_ceiling(journal, parent),
        parent_output_ceiling=source.limits.max_total_output_tokens,
        additional_input_tokens=command.editorial_additional_input_tokens,
        additional_output_tokens=command.editorial_additional_output_tokens,
        elapsed_policy="per-admitted-run/1",
    )
    return binding, manifest, outcome


def verify(journal, command, manifest, materials):
    binding, parent, outcome = source_binding(journal, command, materials)
    if manifest.get(KEY) != binding:
        raise ContractError("editorial_continuation_binding_changed")
    from .manifest import verify_assembly

    verify_assembly(command, parent, binding, manifest)
    return outcome


def seed(journal, manifest):
    binding = manifest[KEY]
    parent = UUID(binding["parent_run_id"])
    parent_manifest = journal.manifest(parent)
    outcome = read_outcome(journal, parent)
    if (
        digest(parent_manifest) != binding["parent_manifest_hash"]
        or outcome is None
        or digest(outcome) != binding["outcome_hash"]
        or outcome["script_ref"] != binding["script_ref"]
        or outcome["script_hash"] != binding["script_hash"]
        or outcome["reading_refs"] != binding["reading_refs"]
    ):
        raise ContractError("editorial_continuation_seed_changed")
    from .manifest import verify_assembly

    verify_assembly(WritingRunCommand.model_validate(manifest["command"]), parent_manifest, binding, manifest)
    script = journal.read(RuntimeRef.model_validate(binding["script_ref"]))["output"]
    readings = {
        kind: journal.read(RuntimeRef.model_validate(ref))["output"]
        for kind, ref in binding["reading_refs"].items()
    }
    return script, binding["script_ref"], readings, binding["reading_refs"]


def own_plan(journal, manifest):
    plan = manifest[KEY]["accepted_plan"]
    if plan is None:
        return None
    return dict(
        accepted_plan=journal.read(RuntimeRef.model_validate(plan["candidate_ref"]))["output"],
        accepted_plan_ref=plan["candidate_ref"],
        planning_clearance_ref=plan["clearance_ref"],
        plan_review_ref=plan["review_ref"],
        accepted_plan_hash=plan["candidate_hash"],
    )


def previous_reader_source(journal, run_id):
    """Explicit parent lookup for the first local reread; no implicit cross-run journal fallback."""
    manifest = journal.manifest(run_id)
    binding = manifest.get(KEY)
    if binding is None:
        return None
    parent = UUID(binding["parent_run_id"])
    source_command = WritingRunCommand.model_validate(journal.manifest(parent)["command"])
    return parent, f"reader-review-{source_command.max_script_revisions}", binding["reading_refs"]["reader"]
