import copy
import hashlib
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from tests.factories import Factory


def test_unicode_evidence_and_full_closure_use_exact_source_and_representation(repo, scope, producer):
    f = Factory(repo, *scope, producer).setup()
    text = "Префикс 🛰️е\u0301\0 — точный диапазон. Суффикс"
    source, blob = f.source(text)
    profile = f.make(
        "ProcessingProfile",
        dict(
            profile_kind="extraction",
            implementation="test-text",
            version="1",
            configuration={},
            input_profile_refs=[],
        ),
    )
    block = str(uuid4())
    representation = f.make(
        "SourceRepresentation",
        dict(
            source_revision_ref=source.ref.model_dump(mode="json"),
            representation_kind="html_text",
            content_blob=blob.model_dump(mode="json"),
            canonical_text_blob=blob.model_dump(mode="json"),
            extractor=dict(
                profile_ref=profile.ref.model_dump(mode="json"),
                name="test-text",
                version="1",
                configuration_hash=digest({}),
            ),
            normalization_version="test-preserve-original-v1",
            blocks=[
                dict(
                    block_id=block,
                    kind="paragraph",
                    text_range=[0, len(text)],
                    original_anchor={"test_data": True},
                    warnings=[],
                )
            ],
            extraction_report=dict(
                coverage="complete", missing_regions=[], reading_order_issues=[], warnings=[]
            ),
            language="ru",
            is_transformation=False,
        ),
    )
    exact = "🛰️е\u0301\0"
    start = text.index(exact)
    selector = dict(
        kind="text",
        block_ids=[block],
        char_start=start,
        char_end=start + len(exact),
        exact=exact,
        prefix="Префикс ",
        suffix=" —",
    )
    context = dict(
        kind="text", block_ids=[block], char_start=0, char_end=len(text), exact=text, prefix="", suffix=""
    )
    payload = dict(
        source_revision_ref=source.ref.model_dump(mode="json"),
        representation_ref=representation.ref.model_dump(mode="json"),
        selector=selector,
        exact_text=exact,
        exact_text_hash=hashlib.sha256(exact.encode()).hexdigest(),
        context_selector=context,
        extraction_warnings=[],
        transformation="none",
    )
    evidence = f.make("EvidenceSpan", payload)
    assert repo.get(f.project, evidence.ref).payload["exact_text"] == exact
    broken = copy.deepcopy(payload)
    broken["selector"]["char_end"] -= 1
    with pytest.raises(ValueError, match="selector_mismatch"):
        f.make("EvidenceSpan", broken)
    note = f.make(
        "ResearchNote",
        f.note().payload
        | {
            "citations": [
                dict(
                    marker="G1",
                    evidence_refs=[evidence.ref.model_dump(mode="json")],
                    relation="supports",
                    explanation=None,
                )
            ]
        },
    )
    raw = dict(
        episode_spec_ref=f.spec.ref.model_dump(mode="json"),
        knowledge_as_of=f.spec.payload["temporal"]["knowledge_as_of"],
        source_policy_ref=f.policies["source"],
        overview_note_ref=note.ref.model_dump(mode="json"),
        timeline_note_ref=None,
        note_refs=[note.ref.model_dump(mode="json")],
        question_refs=[],
        uncertainty_refs=[],
        source_revision_refs=[source.ref.model_dump(mode="json")],
        evidence_refs=[evidence.ref.model_dump(mode="json")],
        candidate_materials=[],
        excluded_topics=[],
    )
    assert f.make("DossierSnapshot", raw).payload == raw
    with pytest.raises(ContractError, match="dossier_evidence_closure_mismatch"):
        f.make("DossierSnapshot", raw | {"evidence_refs": []})
    fresh = f.note()
    fixed = f.dossier(fresh)
    assert fixed.payload["evidence_refs"] == []
