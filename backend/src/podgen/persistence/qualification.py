"""Qualification is an exact measured cell, never a Cartesian product or companion superset."""

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import ContractError

from .database import json_text


def validate_qualification(repo, tx, project, record, reports):
    exact = record.subject_ref.model_dump(mode="json")
    capabilities = [
        r for r in reports if r["kind"] == "CapabilityReport" and r["payload"]["profile_ref"] == exact
    ]
    results = [
        r
        for r in reports
        if r["kind"] == "EvaluationResult" and exact in r["payload"]["candidate_profile_refs"]
    ]
    if not capabilities or not results:
        raise ContractError("qualification_requires_exact_capability_and_evaluation")
    if any(
        not r["payload"]["checks"] or any(c["outcome"] != "pass" for c in r["payload"]["checks"])
        for r in capabilities
    ):
        raise ContractError("qualification_capabilities_not_passed")
    if any(r["payload"]["outcome"] != "pass" for r in results):
        raise ContractError("qualification_assessment_not_passed")
    if not record.assessment_scope.supported_cells:
        raise ContractError("qualification_requires_scoped_assessment")
    for cell in record.assessment_scope.supported_cells:
        interval = cell.duration_interval
        # Interpolation needs calibrated policy/evidence, which WP-02 has not supplied yet.
        if interval.min_seconds != interval.max_seconds:
            raise ContractError("qualification_interval_requires_calibrated_policy")
        wanted = {json_text(exact)} | {json_text(r) for r in cell.companion_profile_refs}
        for companion in cell.companion_profile_refs:
            repo._resolve(tx, project, companion)
        matching = False
        for result in results:
            body = result["payload"]
            case = repo._resolve(tx, project, ArtifactRef.model_validate(body["case_ref"]))
            if case["kind"] != "EvaluationCase":
                continue
            combination = case["payload"]["supported_combination"]
            tested_roles = set()
            for measurement in body["measurements"]:
                tested_roles.update(measurement.get("tested_roles", []))
            actual = {json_text(r) for r in body["candidate_profile_refs"]}
            allowed = {json_text(r) for r in case["payload"]["allowed_candidate_profile_refs"]}
            if (
                combination
                == dict(language=cell.language, format=cell.format, duration_seconds=interval.min_seconds)
                and actual == wanted
                and actual <= allowed
                and cell.roles
                and set(cell.roles) <= tested_roles
            ):
                matching = True
        if not matching:
            raise ContractError("qualification_cell_has_no_matching_evaluation")
