"""Two isolated readings per exact version, bounded rewrites, and an immutable terminal outcome."""

from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.writing.reading_audit.consistency import from_manifest as consistency_from_manifest

from .contracts import RunFailure
from .reading_stage import execute_reading_stage
from .reading_workflow import VERSION, round_stage
from .script_import import load_script
from .stage import execute_stage


def outcome_ref(run_id):
    return RuntimeRef(record_kind="tool_execution", record_id=uuid5(run_id, "independent-reading-outcome"))


def combine(readings, *, consistency_contract=None):
    from podgen.writing.reading_audit.consistency import validate

    for review in readings.values():
        validate(review, consistency_contract)
    if any(r["research_requests"] or r["recommendation"] == "needs_material" for r in readings.values()):
        return dict(recommendation="needs_material", reason="necessary_material_requested")
    if any(
        r["recommendation"] == "revise"
        or any(f["priority"] in ("blocking", "important") for f in r["findings"])
        for r in readings.values()
    ):
        return dict(recommendation="revise", reason="required_changes_from_independent_readings")
    return dict(recommendation="keep", reason="both_independent_readings_keep")


def execute_reading_cycle(
    *,
    journal,
    gateway,
    generation,
    command,
    manifest,
    materials,
    outputs,
    records,
    totals,
    guard,
    own_plan,
    repair_review_anchors=False,
):
    common = dict(
        journal=journal,
        gateway=gateway,
        generation=generation,
        command=command,
        manifest=manifest,
        materials=materials,
        totals=totals,
        guard=guard,
    )
    from .editorial_continuation.source import KEY, offset, seed

    continuing = KEY in manifest

    def rewrite(index, script, script_ref, readings, refs):
        stage = round_stage("revise", index)
        guard()
        result = execute_reading_stage(
            stage,
            **common,
            script=script,
            script_ref=script_ref,
            readings=readings,
            reading_refs=refs,
            decision=combine(readings, consistency_contract=consistency_from_manifest(manifest)),
            approved_plan=own_plan["accepted_plan"] if own_plan else None,
        )
        outputs[stage], records[stage] = result.output, result.ref.model_dump(mode="json")
        totals["input"] += result.input_tokens
        totals["output"] += result.output_tokens_or_reservation
        return result.output, records[stage]

    if continuing:
        totals.update(manifest[KEY]["inherited_usage"])
        script, script_ref, readings, refs = seed(journal, manifest)
        outputs["draft"], records["draft"] = script, script_ref
        script, script_ref = rewrite(1, script, script_ref, readings, refs)
    elif command.initial_script_ref is not None:
        script = load_script(
            journal.repo, command.project_id, command.initial_script_ref, materials.speaker_labels
        )
        script_ref = command.initial_script_ref.model_dump(mode="json")
        if digest(script) != manifest["initial_script_hash"]:
            raise RunFailure("initial_script_changed", run_id=command.run_id)
        outputs["draft"], records["draft"] = script, script_ref
    else:
        initial = execute_stage("draft", **common, outputs=outputs, records=records, own_plan=own_plan)
        script, script_ref = initial.output, initial.ref.model_dump(mode="json")
        outputs["draft"], records["draft"] = script, script_ref
        totals["input"] += initial.input_tokens
        totals["output"] += initial.output_tokens_or_reservation
    rounds = []
    for index in range(1 if continuing else 0, command.max_script_revisions + 1):
        readings, refs = {}, {}
        for origin, base in (("reader", "reader-review"), ("fact", "fact-review")):
            guard()
            stage = round_stage(base, index)
            result = execute_reading_stage(
                stage,
                **common,
                script=script,
                script_ref=script_ref,
                approved_plan=own_plan["accepted_plan"] if origin == "fact" and own_plan else None,
                repair_review_anchors=repair_review_anchors,
            )
            readings[origin], refs[origin] = result.output, result.ref.model_dump(mode="json")
            outputs[stage], records[stage] = result.output, refs[origin]
            totals["input"] += result.input_tokens
            totals["output"] += result.output_tokens_or_reservation
        decision = combine(readings, consistency_contract=consistency_from_manifest(manifest))
        round_record = dict(
            contract="independent-reading-round/1",
            review_workflow=VERSION,
            run_id=str(command.run_id),
            index=index,
            script_ref=script_ref,
            script_hash=digest(script),
            reading_refs=refs,
            decision=decision,
        )
        if continuing:
            round_record["global_revision_index"] = offset(manifest) + index
        ref = journal.record(round_record, record_id=uuid5(command.run_id, f"reading-round:{index}"))
        rounds.append(ref.model_dump(mode="json"))
        terminal = decision["recommendation"] != "revise" or index == command.max_script_revisions
        if terminal:
            status = decision["recommendation"]
            if status == "revise":
                status = "revision_limit_exhausted"
            outcome = dict(
                contract="independent-reading-outcome/1",
                review_workflow=VERSION,
                run_id=str(command.run_id),
                status=status,
                script_ref=script_ref,
                script_hash=digest(script),
                reading_refs=refs,
                rounds=rounds,
                output_refs=dict(records),
                token_usage_or_reservation=dict(totals),
                output=script,
                semantic_acceptance=False,
                production_ready=False,
            )
            if continuing:
                from .editorial_continuation.budget import split_usage

                outcome.update(
                    revisions_completed=offset(manifest) + index,
                    max_total_revisions=manifest[KEY]["max_total_revisions"],
                    usage_ledger=split_usage(manifest, totals),
                )
            journal.record(outcome, record_id=outcome_ref(command.run_id).record_id)
            from .reading_validation import certify

            return certify(journal, command.run_id)
        script, script_ref = rewrite(index + 1, script, script_ref, readings, refs)
