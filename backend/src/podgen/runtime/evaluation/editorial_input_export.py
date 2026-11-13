"""Export real input provenance: original imported bytes or a verified journal clearance."""

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.editorial_contract import EditorialInput

from .journal import EvaluationJournal
from .reusable_plan import load_approved_plan


def export_inputs(repo, project, manifest, *, write_json, write_bytes):
    command = manifest["command"]
    shared_ref = RuntimeRef.model_validate(command["author_ref"])
    shared = repo.get_runtime(project, shared_ref)
    if shared.get("contract") == "persisted-research-editorial-input/1":
        from podgen.research.materials.export import export_materials

        packet = export_materials(
            repo, project, manifest, shared_ref, shared, write_json=write_json, write_bytes=write_bytes
        )
    else:
        if shared.get("contract") != "persisted-editorial-input/1":
            raise ContractError("editorial_input_projection_required")
        packet = EditorialInput.model_validate(shared["content"])
        write_bytes(
            "editorial-input-original.json",
            repo.read_runtime_blob(
                project, shared_ref, BlobRef.model_validate(shared["original_input_blob"])
            ),
        )
    value = command.get("fixed_plan_input_ref")
    if value is None:
        return
    fixed_ref = RuntimeRef.model_validate(value)
    saved = repo.get_runtime(project, fixed_ref)
    if saved.get("contract") == "persisted-fixed-editorial-input/1":
        if saved["shared_input_ref"] != shared_ref.model_dump(mode="json"):
            raise ContractError("fixed_editorial_input_binding_mismatch")
        write_bytes(
            "fixed-editorial-input-original.json",
            repo.read_runtime_blob(project, fixed_ref, BlobRef.model_validate(saved["original_input_blob"])),
        )
        return
    if saved.get("contract") != "writing-planning-clearance-v1":
        raise ContractError("unsupported_fixed_plan_export_source")
    approved = load_approved_plan(repo, project, shared_ref, packet, fixed_ref)
    journal = EvaluationJournal(repo, project)
    review_ref = RuntimeRef.model_validate(saved["plan_review_ref"])
    write_json("fixed-editorial-plan.json", approved.model_dump(mode="json"))
    write_json("fixed-plan-source/clearance.json", saved)
    write_json("fixed-plan-source/candidate.json", journal.read(approved.candidate_ref))
    write_json("fixed-plan-source/review.json", journal.read(review_ref))
    write_json("fixed-plan-source/manifest.json", journal.manifest(approved.source_run_id))
    write_json(
        "fixed-plan-origin.json",
        dict(
            contract="journal-approved-plan-export/1",
            source_run_id=str(approved.source_run_id),
            shared_input_ref=shared_ref.model_dump(mode="json"),
            clearance_ref=fixed_ref.model_dump(mode="json"),
            candidate_ref=approved.candidate_ref.model_dump(mode="json"),
            candidate_hash=approved.candidate_hash,
            review_ref=review_ref.model_dump(mode="json"),
            files=dict(
                plan="fixed-editorial-plan.json",
                clearance="fixed-plan-source/clearance.json",
                candidate="fixed-plan-source/candidate.json",
                review="fixed-plan-source/review.json",
                manifest="fixed-plan-source/manifest.json",
            ),
        ),
    )
