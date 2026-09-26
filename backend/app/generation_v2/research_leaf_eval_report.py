from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.research_workspace_prompt_payloads import research_job_prompt_payload
from app.models.generation_v2 import EvidenceItem


def job_report_evidence_ids(job: dict[str, Any]) -> list[str]:
    evidence_ids: list[str] = [str(item) for item in job.get("evidence_ids") or [] if str(item).strip()]
    for outcome in job.get("requirement_outcomes") or []:
        if isinstance(outcome, dict):
            evidence_ids.extend(str(item) for item in outcome.get("evidence_ids") or [] if str(item).strip())
    for outcome in job.get("question_outcomes") or []:
        if isinstance(outcome, dict):
            evidence_ids.extend(str(item) for item in outcome.get("evidence_ids") or [] if str(item).strip())
    for observation in job.get("observations") or []:
        if isinstance(observation, dict):
            evidence_ids.extend(str(item) for item in observation.get("evidence_ids") or [] if str(item).strip())
    for finding in job.get("source_findings") or []:
        if isinstance(finding, dict):
            evidence_ids.extend(str(item) for item in finding.get("evidence_ids") or [] if str(item).strip())
    return sorted(set(evidence_ids))


async def load_evidence_text_by_id(db: Any, *, run_id: uuid.UUID, evidence_ids: list[str]) -> dict[str, str]:
    valid_ids: list[uuid.UUID] = []
    for evidence_id in evidence_ids:
        try:
            valid_ids.append(uuid.UUID(str(evidence_id)))
        except ValueError:
            continue
    if not valid_ids:
        return {}
    result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == run_id,
            EvidenceItem.id.in_(valid_ids),
        )
    )
    return {str(item.id): str(item.snippet_text or "") for item in result.scalars()}


def job_for_question(workspace: dict[str, Any], question_id: str) -> dict[str, Any] | None:
    for job in workspace.get("investigation_jobs") or []:
        if not isinstance(job, dict):
            continue
        if question_id in {str(item) for item in job.get("coverage_question_ids") or []}:
            return job
    return None


def job_prompt_size_report(
    workspace: dict[str, Any],
    job: dict[str, Any],
    *,
    source_map_summary: dict[str, Any],
) -> dict[str, Any]:
    payload = research_job_prompt_payload(
        workspace,
        str(job.get("job_id") or ""),
        source_map_summary=source_map_summary,
    )
    return {
        "prompt_payload_chars": len(json.dumps(payload, ensure_ascii=False)),
        "source_finding_count": len(job.get("source_findings") or []),
        "source_gap_count": len(job.get("source_gaps") or []),
        "source_card_count": len(job.get("source_cards") or []),
        "initial_payload_has_full_source_findings": _contains_key(payload, "source_mapping_findings"),
        "initial_payload_has_full_source_gaps": _contains_key(payload, "source_mapping_gaps"),
        "initial_payload_has_full_source_cards": _contains_key(payload, "source_mapping_cards"),
    }


def leaf_job_structural_report(
    workspace: dict[str, Any],
    job: dict[str, Any],
    *,
    evidence_text_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    requirements = [item for item in job.get("coverage_requirements") or [] if isinstance(item, dict)]
    requirement_outcomes = [item for item in job.get("requirement_outcomes") or [] if isinstance(item, dict)]
    requirement_ids = {str(item.get("requirement_id") or "") for item in requirements if str(item.get("requirement_id") or "")}
    outcome_ids = {str(item.get("requirement_id") or "") for item in requirement_outcomes if str(item.get("requirement_id") or "")}
    deferred_requirement_ids = sorted(
        str(item.get("requirement_id") or "")
        for item in requirement_outcomes
        if str(item.get("status") or "") == "deferred" and str(item.get("requirement_id") or "")
    )
    answered_requirement_ids = sorted(
        str(item.get("requirement_id") or "")
        for item in requirement_outcomes
        if str(item.get("status") or "") == "answered" and str(item.get("requirement_id") or "")
    )
    issues: list[str] = []
    if requirement_ids != outcome_ids:
        issues.append("requirement outcomes do not exactly match coverage requirements")
    job_status = str(job.get("status") or "")
    if job_status not in {"mapped", "deferred"}:
        issues.append("job did not reach terminal status")
    if job_status == "mapped" and deferred_requirement_ids:
        issues.append("mapped job has deferred requirements")
    deferred_without_gap = [
        requirement_id
        for requirement_id in deferred_requirement_ids
        if not _has_requirement_gap(workspace, job, requirement_id)
    ]
    if deferred_without_gap:
        issues.append("deferred requirements are missing persisted research gaps")
    if not (job.get("observations") or job.get("atom_ids") or job.get("evidence_ids")):
        issues.append("leaf canary made no persisted research progress")
    atom_ids = [str(item) for item in job.get("atom_ids") or [] if str(item).strip()]
    atoms_by_id = {
        str(item.get("atom_id") or ""): item
        for item in workspace.get("atom_drafts") or []
        if isinstance(item, dict) and str(item.get("atom_id") or "")
    }
    atom_previews = [
        {
            "atom_id": atom_id,
            "kind": atoms_by_id.get(atom_id, {}).get("kind"),
            "story_weight": atoms_by_id.get(atom_id, {}).get("story_weight"),
            "title": atoms_by_id.get(atom_id, {}).get("title"),
            "claim": atoms_by_id.get(atom_id, {}).get("claim"),
            "evidence_ids": atoms_by_id.get(atom_id, {}).get("evidence_ids") or [],
        }
        for atom_id in atom_ids
    ]
    observation_previews = [
        {
            "note": item.get("note"),
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": item.get("evidence_ids") or [],
        }
        for item in job.get("observations") or []
        if isinstance(item, dict)
    ]
    semantic_canary = semantic_leaf_canary_report(
        workspace,
        job,
        requirement_outcomes=requirement_outcomes,
        evidence_text_by_id=evidence_text_by_id or {},
    )
    if semantic_canary["issues"]:
        issues.extend(f"semantic canary: {issue}" for issue in semantic_canary["issues"])
    return {
        "passed": not issues,
        "issues": issues,
        "semantic_canary": semantic_canary,
        "coverage_requirement_count": len(requirements),
        "requirement_outcome_count": len(requirement_outcomes),
        "answered_requirement_ids": answered_requirement_ids,
        "deferred_requirement_ids": deferred_requirement_ids,
        "deferred_requirement_ids_without_gap": deferred_without_gap,
        "requirement_outcomes": [
            {
                "requirement_id": item.get("requirement_id"),
                "status": item.get("status"),
                "note": item.get("note"),
                "evidence_ids": item.get("evidence_ids") or [],
                "supporting_atom_ids": item.get("supporting_atom_ids") or [],
            }
            for item in requirement_outcomes
        ],
        "question_outcomes": [
            {
                "question": item.get("question"),
                "status": item.get("status"),
                "note": item.get("note"),
                "evidence_ids": item.get("evidence_ids") or [],
            }
            for item in job.get("question_outcomes") or []
            if isinstance(item, dict)
        ],
        "question_outcome_count": len(job.get("question_outcomes") or []),
        "atom_count": len(job.get("atom_ids") or []),
        "atom_previews": atom_previews,
        "observation_count": len(job.get("observations") or []),
        "observation_previews": observation_previews,
    }


def semantic_leaf_canary_report(
    workspace: dict[str, Any],
    job: dict[str, Any],
    *,
    requirement_outcomes: list[dict[str, Any]],
    evidence_text_by_id: dict[str, str],
) -> dict[str, Any]:
    question_ids = {str(item) for item in job.get("coverage_question_ids") or [] if str(item)}
    if "q_hkd_current_mechanics" not in question_ids:
        return {"applied": False, "issues": [], "checks": []}
    outcome_by_id = {
        str(item.get("requirement_id") or ""): item
        for item in requirement_outcomes
        if str(item.get("requirement_id") or "")
    }
    expectations = [
        {
            "requirement_id": "q_hkd_mech_r1",
            "mode": "answered",
            "term_groups": [
                ["HSBC", "Hongkong and Shanghai", "Hong Kong and Shanghai", "Hong Kong Shanghai", "Гонконгск"],
                ["Standard Chartered", "Стандард"],
                ["Bank of China", "Банк Китая"],
                ["HK$20", "$20", "20 (blue)", "номинал 20"],
                ["HK$50", "$50", "50 (green)", "номинал 50"],
                ["HK$100", "$100", "100 (red)", "номинал 100"],
                ["HK$500", "$500", "500 (brown)", "номинал 500"],
                ["HK$1,000", "HK$1000", "$1,000", "$1000", "1,000 (gold)", "номинал 1 000", "номинал 1000"],
            ],
        },
        {
            "requirement_id": "q_hkd_mech_r2",
            "mode": "answered",
            "term_groups": [
                ["HKMA", "Hong Kong Monetary Authority"],
                ["Exchange Fund", "Валютн"],
                ["Certificate of Indebtedness", "certificates of indebtedness", "сертификат", "обеспеч"],
            ],
        },
        {
            "requirement_id": "q_hkd_mech_r3",
            "mode": "answered",
            "term_groups": [
                ["LERS", "linked exchange rate", "currency board", "связанн"],
                ["7.80", "7,80", "7.8", "7,8"],
                ["issuance", "issue", "banknote", "банкнот", "эмисс"],
            ],
        },
        {
            "requirement_id": "q_hkd_mech_r4",
            "mode": "answered_or_deferred_gap",
            "term_groups": [
                ["HK$10", "$10", "10 dollar", "10-dollar", "10-доллар", "десятк"],
                ["why", "reason", "because", "почему", "причин", "объясн", "логик", "механизм"],
                ["HKMA", "government", "state", "государств", "правительств"],
                [
                    "private bank",
                    "private banks",
                    "commercial bank",
                    "commercial banks",
                    "note-issuing bank",
                    "note-issuing banks",
                    "частный банк",
                    "частного банка",
                    "частному банку",
                    "частным банком",
                    "частном банке",
                    "частные банки",
                    "частных банков",
                    "частным банкам",
                    "частными банками",
                    "частных банках",
                    "коммерческий банк",
                    "коммерческого банка",
                    "коммерческому банку",
                    "коммерческим банком",
                    "коммерческом банке",
                    "коммерческие банки",
                    "коммерческих банков",
                    "коммерческим банкам",
                    "коммерческими банками",
                    "коммерческих банках",
                    "банк-эмитент",
                    "банка-эмитента",
                    "банку-эмитенту",
                    "банком-эмитентом",
                    "банке-эмитенте",
                    "банки-эмитенты",
                    "банков-эмитентов",
                    "банкам-эмитентам",
                    "банками-эмитентами",
                    "банках-эмитентах",
                ],
            ],
        },
    ]
    checks: list[dict[str, Any]] = []
    issues: list[str] = []
    for expectation in expectations:
        requirement_id = str(expectation["requirement_id"])
        outcome = outcome_by_id.get(requirement_id) or {}
        status = str(outcome.get("status") or "")
        evidence_ids = [str(item) for item in outcome.get("evidence_ids") or [] if str(item)]
        gap_text = _requirement_gap_text(workspace, job, requirement_id)
        evidence_backed_text = _evidence_text_for_ids(evidence_text_by_id, evidence_ids)
        terms_text = gap_text if status == "deferred" else evidence_backed_text
        terms_ok = _matches_term_groups(terms_text, expectation["term_groups"])
        gap_ok = _has_requirement_gap(workspace, job, requirement_id)
        check = {
            "requirement_id": requirement_id,
            "expected": expectation["mode"],
            "status": status,
            "evidence_count": len(evidence_ids),
            "evidence_backed_text_chars": len(evidence_backed_text),
            "term_groups_matched": terms_ok,
            "has_open_requirement_gap": gap_ok,
        }
        checks.append(check)
        atom_check = None
        if status == "answered":
            atom_check = answered_requirement_atom_check(
                workspace,
                job,
                requirement_id=requirement_id,
                evidence_ids=evidence_ids,
                supporting_atom_ids=[
                    str(item) for item in outcome.get("supporting_atom_ids") or [] if str(item).strip()
                ],
                evidence_text_by_id=evidence_text_by_id,
                term_groups=expectation["term_groups"],
            )
            checks.append(atom_check)
        if expectation["mode"] == "answered":
            if status != "answered":
                issues.append(f"{requirement_id} must be answered")
            if not evidence_ids:
                issues.append(f"{requirement_id} answered outcome needs evidence")
            if evidence_ids and not evidence_backed_text.strip():
                issues.append(f"{requirement_id} answered outcome needs evidence-backed persisted material")
            if not terms_ok:
                issues.append(f"{requirement_id} evidence-backed material is missing expected domain terms")
            if atom_check is None or not atom_check["passed"]:
                issues.append(f"{requirement_id} answered outcome needs an authored atom using the same evidence")
        elif status == "answered":
            if not evidence_ids:
                issues.append(f"{requirement_id} answered outcome needs evidence")
            if evidence_ids and not evidence_backed_text.strip():
                issues.append(f"{requirement_id} answered outcome needs evidence-backed persisted material")
            if not terms_ok:
                issues.append(f"{requirement_id} evidence-backed material is missing expected residual HK$10 terms")
            if atom_check is None or not atom_check["passed"]:
                issues.append(f"{requirement_id} answered outcome needs an authored atom using the same evidence")
        elif status == "deferred":
            if not gap_ok:
                issues.append(f"{requirement_id} deferred outcome needs an open_requirement gap")
            if not terms_ok:
                issues.append(f"{requirement_id} deferred outcome/gap text is missing expected residual HK$10 terms")
        else:
            issues.append(f"{requirement_id} must be answered or honestly deferred")
    mechanism_check = _job_mechanism_atom_check(workspace, job)
    checks.append(mechanism_check)
    if not mechanism_check["passed"]:
        issues.append("job needs at least one authored mechanism atom with evidence")
    russian_prose_check = _job_russian_atom_prose_check(workspace, job)
    checks.append(russian_prose_check)
    if not russian_prose_check["passed"]:
        issues.append("job atom prose should be written in requested Russian language")
    return {"applied": True, "issues": issues, "checks": checks}


def answered_requirement_atom_check(
    workspace: dict[str, Any],
    job: dict[str, Any],
    *,
    requirement_id: str,
    evidence_ids: list[str],
    supporting_atom_ids: list[str],
    evidence_text_by_id: dict[str, str],
    term_groups: list[list[str]],
) -> dict[str, Any]:
    required_evidence_ids = {str(item) for item in evidence_ids if str(item).strip()}
    explicit_atom_ids = {str(item) for item in supporting_atom_ids if str(item).strip()}
    if not explicit_atom_ids:
        return {
            "kind": "answered_requirement_atom",
            "requirement_id": requirement_id,
            "passed": False,
            "atom_count": 0,
            "supporting_atom_ids": [],
            "candidate_evidence_ids": sorted(required_evidence_ids),
            "shared_evidence_ids": [],
            "evidence_text_chars": 0,
            "atom_terms_matched": False,
            "evidence_terms_matched": False,
            "error": "answered requirement must list supporting_atom_ids explicitly",
        }
    matched_atoms: list[dict[str, Any]] = []
    shared_evidence_ids: set[str] = set()
    for atom in _job_atoms(workspace, job):
        if str(atom.get("atom_id") or "") not in explicit_atom_ids:
            continue
        atom_shared_evidence_ids = required_evidence_ids & set(_atom_evidence_ids(atom))
        if not atom_shared_evidence_ids:
            continue
        matched_atoms.append(atom)
        shared_evidence_ids.update(atom_shared_evidence_ids)
    atom_text = "\n".join(_atom_text(atom) for atom in matched_atoms)
    evidence_text = _evidence_text_for_ids(evidence_text_by_id, sorted(shared_evidence_ids))
    atom_terms_ok = _matches_term_groups(atom_text, term_groups)
    evidence_terms_ok = _matches_term_groups(evidence_text, term_groups)
    if matched_atoms and shared_evidence_ids and atom_terms_ok and evidence_terms_ok:
        return {
            "kind": "answered_requirement_atom",
            "requirement_id": requirement_id,
            "passed": True,
            "atom_ids": [str(atom.get("atom_id") or "") for atom in matched_atoms],
            "supporting_atom_ids": sorted(explicit_atom_ids),
            "shared_evidence_ids": sorted(shared_evidence_ids),
            "evidence_text_chars": len(evidence_text),
        }
    return {
        "kind": "answered_requirement_atom",
        "requirement_id": requirement_id,
        "passed": False,
        "atom_count": len(matched_atoms),
        "supporting_atom_ids": sorted(explicit_atom_ids),
        "candidate_evidence_ids": sorted(required_evidence_ids),
        "shared_evidence_ids": sorted(shared_evidence_ids),
        "evidence_text_chars": len(evidence_text),
        "atom_terms_matched": atom_terms_ok,
        "evidence_terms_matched": evidence_terms_ok,
    }


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _matches_term_groups(text: str, groups: list[list[str]]) -> bool:
    normalized = text.casefold()
    return all(any(_matches_term(normalized, str(term)) for term in group) for group in groups)


def _matches_term(normalized_text: str, term: str) -> bool:
    normalized_term = term.casefold().strip()
    if not normalized_term:
        return False
    if "$" in normalized_term:
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![0-9])", normalized_text) is not None
    if normalized_term[-1].isdigit():
        return re.search(rf"{re.escape(normalized_term)}(?![0-9])", normalized_text) is not None
    if re.search(r"\bbanks?\b", normalized_term):
        return re.search(rf"(?<![a-z]){re.escape(normalized_term)}(?![a-z])", normalized_text) is not None
    if "банк" in normalized_term:
        return (
            re.search(rf"(?<![а-яё]){re.escape(normalized_term)}(?![а-яё])", normalized_text)
            is not None
        )
    return normalized_term in normalized_text


def _evidence_text_for_ids(evidence_text_by_id: dict[str, str], evidence_ids: list[str]) -> str:
    return "\n".join(
        str(evidence_text_by_id.get(str(evidence_id)) or "")
        for evidence_id in evidence_ids
        if str(evidence_text_by_id.get(str(evidence_id)) or "").strip()
    )


def _job_atoms(workspace: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    job_id = str(job.get("job_id") or "")
    job_atom_ids = {str(item) for item in job.get("atom_ids") or [] if str(item).strip()}
    atoms: list[dict[str, Any]] = []
    for atom in workspace.get("atom_drafts") or []:
        if not isinstance(atom, dict):
            continue
        atom_job_id = str(atom.get("job_id") or "")
        atom_id = str(atom.get("atom_id") or "")
        if job_atom_ids:
            if atom_id not in job_atom_ids:
                continue
            if job_id and atom_job_id and atom_job_id != job_id:
                continue
        elif job_id and atom_job_id != job_id:
            continue
        atoms.append(atom)
    return atoms


def _atom_evidence_ids(atom: dict[str, Any]) -> list[str]:
    return [str(item) for item in atom.get("evidence_ids") or [] if str(item).strip()]


def _atom_text(atom: dict[str, Any]) -> str:
    return "\n".join(
        [
            str(atom.get("title") or ""),
            str(atom.get("claim") or ""),
            str(atom.get("explanation") or ""),
            str(atom.get("story_value") or ""),
        ]
    )


def _atom_authored_text(atom: dict[str, Any]) -> str:
    return "\n".join(
        str(atom.get(key) or "")
        for key in (
            "title",
            "claim",
            "explanation",
            "story_value",
            "risk",
            "treatment_role_hint",
        )
    )


def _job_russian_atom_prose_check(workspace: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    offenders: list[dict[str, Any]] = []
    for atom in _job_atoms(workspace, job):
        text = _atom_authored_text(atom)
        cyrillic_count = sum(1 for char in text.casefold() if "а" <= char <= "я" or char == "ё")
        latin_count = sum(1 for char in text.casefold() if "a" <= char <= "z")
        if cyrillic_count < 20 and latin_count >= 20:
            offenders.append(
                {
                    "atom_id": atom.get("atom_id"),
                    "latin_letters": latin_count,
                    "cyrillic_letters": cyrillic_count,
                }
            )
            continue
        if latin_count > max(120, cyrillic_count * 3):
            offenders.append(
                {
                    "atom_id": atom.get("atom_id"),
                    "latin_letters": latin_count,
                    "cyrillic_letters": cyrillic_count,
                }
            )
    return {
        "kind": "russian_atom_prose",
        "passed": not offenders,
        "offenders": offenders,
        "atom_count": len(_job_atoms(workspace, job)),
    }


def _job_mechanism_atom_check(workspace: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    atoms = _job_atoms(workspace, job)
    mechanism_atoms = [
        atom
        for atom in atoms
        if str(atom.get("kind") or "") == "mechanism" and _atom_evidence_ids(atom)
    ]
    return {
        "kind": "job_mechanism_atom",
        "passed": bool(mechanism_atoms),
        "atom_count": len(atoms),
        "mechanism_atom_count": len(mechanism_atoms),
    }


def _requirement_gap_text(workspace: dict[str, Any], job: dict[str, Any], requirement_id: str) -> str:
    job_id = str(job.get("job_id") or "")
    values: list[str] = []
    for gap in workspace.get("research_gaps") or []:
        if not isinstance(gap, dict):
            continue
        if str(gap.get("category") or "") != "open_requirement":
            continue
        if str(gap.get("requirement_id") or "") != requirement_id:
            continue
        if job_id and str(gap.get("job_id") or "") not in {"", job_id}:
            continue
        values.append(str(gap.get("resolution_note") or ""))
    return "\n".join(values)


def _has_requirement_gap(workspace: dict[str, Any], job: dict[str, Any], requirement_id: str) -> bool:
    job_id = str(job.get("job_id") or "")
    for gap in workspace.get("research_gaps") or []:
        if not isinstance(gap, dict):
            continue
        if str(gap.get("category") or "") != "open_requirement":
            continue
        if str(gap.get("status") or "") not in {"open", "deferred"}:
            continue
        if str(gap.get("requirement_id") or "") != requirement_id:
            continue
        if job_id and str(gap.get("job_id") or "") not in {"", job_id}:
            continue
        return True
    return False


__all__ = [
    "answered_requirement_atom_check",
    "job_for_question",
    "job_prompt_size_report",
    "job_report_evidence_ids",
    "leaf_job_structural_report",
    "load_evidence_text_by_id",
]
