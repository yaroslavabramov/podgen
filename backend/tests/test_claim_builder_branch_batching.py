from types import SimpleNamespace
import uuid

import pytest

from app.pipeline.phase4_claims.claim_builder import (
    BUILD_CLAIMS_SYSTEM,
    _build_branch_batches,
    _build_claim_builder_instructions,
    _claim_rebuild_signature,
    _derive_targeted_rebuild_scope,
    _filter_branch_batches,
    _persist_claim_batch,
    _partition_claim_ids_for_targeted_rebuild,
    _prepare_claim_builder_quote,
    _select_claim_assignment,
    build_claims,
)
from app.pipeline.phase4_claims.claim_notes import (
    decode_claim_notes_metadata,
    encode_claim_notes_metadata,
    extract_editor_claim_notes,
)
from app.models.claim import Claim, ClaimEvidence, VerificationLevel


def test_build_branch_batches_uses_contextual_asset_types_per_branch() -> None:
    evidence_id = str(uuid.uuid4())
    evidence_index = {
        evidence_id: {
            "id": evidence_id,
            "verbatim_quote": "Banks issue notes against Certificates of Indebtedness.",
            "canonical_asset_type": "timeline",
            "narrative_value_score": 0.5,
            "reliability_hint": "high",
            "source_id": str(uuid.uuid4()),
            "source": {
                "title": "HKMA explainer",
                "url": "https://example.com",
                "type": "web",
                "domain_reliability": "high",
                "authority_type": "secondary",
                "is_temporary": False,
                "published_at": None,
            },
            "lineage": {
                "branch_ids": [],
                "question_ids": [],
                "intents": [],
                "retrieved_by": [],
                "question_contexts": [],
            },
        }
    }
    provenances = [
        SimpleNamespace(
            evidence_id=uuid.UUID(evidence_id),
            branch_id="b3",
            question_id="b3.1",
            retrieved_by="hunter",
            asset_type="mechanism",
            direct_or_contextual="direct",
        ),
        SimpleNamespace(
            evidence_id=uuid.UUID(evidence_id),
            branch_id="b6",
            question_id="b6.2",
            retrieved_by="librarian_fast",
            asset_type="contrast",
            direct_or_contextual="contextual",
        ),
    ]

    batches = dict(
        _build_branch_batches(
            evidence_index,
            provenances,
            question_context_map={
                "b3.1": {"question": "How does it work?", "intent": "explain"},
                "b6.2": {"question": "Why does it matter?", "intent": "debate"},
            },
        )
    )

    assert batches["b3"][0]["asset_type"] == "mechanism"
    assert batches["b3"][0]["contextual_asset_types"] == ["mechanism"]
    assert batches["b3"][0]["canonical_asset_type"] == "timeline"
    assert batches["b3"][0]["lineage"]["intents"] == ["debate", "explain"]
    assert batches["b3"][0]["lineage"]["question_contexts"][0]["direct_or_contextual"] == "direct"

    assert batches["b6"][0]["asset_type"] == "contrast"
    assert batches["b6"][0]["contextual_asset_types"] == ["contrast"]
    assert batches["b6"][0]["lineage"]["branch_ids"] == ["b3", "b6"]
    assert batches["b6"][0]["lineage"]["question_contexts"][1]["direct_or_contextual"] == "contextual"


def test_derive_targeted_rebuild_scope_only_activates_inside_verify_loop() -> None:
    assert _derive_targeted_rebuild_scope({"iteration_counts": {}, "research_gaps": []}) is None

    scope = _derive_targeted_rebuild_scope(
        {
            "iteration_counts": {"verify_research_loops": 1},
            "research_gaps": [
                {"branch_id": "b3", "claim_id": "c1"},
                {"branch_id": "b6", "claim_id": "c2"},
            ],
        }
    )

    assert scope == {"branch_ids": ["b3", "b6"], "claim_ids": ["c1", "c2"]}


def test_derive_targeted_rebuild_scope_expands_merged_gap_branch_and_claim_ids() -> None:
    scope = _derive_targeted_rebuild_scope(
        {
            "iteration_counts": {"verify_research_loops": 1},
            "research_gaps": [
                {
                    "branch_id": "b6",
                    "claim_id": "c1",
                    "claim_ids": ["c1", "c2"],
                    "question_contexts": [
                        {"branch_id": "b6", "question_id": "b6.2"},
                        {"branch_id": "b7", "question_id": "b7.1"},
                    ],
                }
            ],
        }
    )

    assert scope == {"branch_ids": ["b6", "b7"], "claim_ids": ["c1", "c2"]}


def test_filter_branch_batches_keeps_only_targeted_branches() -> None:
    branch_batches = [("b1", [{"id": "e1"}]), ("b3", [{"id": "e2"}]), ("b6", [{"id": "e3"}])]
    assert _filter_branch_batches(branch_batches, ["b3", "b6"]) == [("b3", [{"id": "e2"}]), ("b6", [{"id": "e3"}])]


def test_build_claim_builder_instructions_adds_debate_specific_guardrails() -> None:
    instructions = _build_claim_builder_instructions({"branch_intent": "debate"})
    assert "writer-usable claims" in instructions
    assert "Do not turn every evidence span or supporting detail into its own claim" in instructions
    assert "tradeoffs" in instructions
    assert "synthetic argumentative claims" in instructions
    assert "Preserve one argumentative position as one claim" in instructions
    assert "Do not infer dates, motives, purposes, or causal links" in instructions


def test_build_claims_system_prefers_compact_non_micro_claims() -> None:
    assert "smallest high-value, non-redundant set of claims" in BUILD_CLAIMS_SYSTEM
    assert "Do not create one claim per evidence span by default" in BUILD_CLAIMS_SYSTEM
    assert "micro-variants of the same proposition" in BUILD_CLAIMS_SYSTEM
    assert "prefer merge if the resulting claim remains semantically clean and directly auditable" in BUILD_CLAIMS_SYSTEM
    assert "Evidence IDs are opaque labels" in BUILD_CLAIMS_SYSTEM


def test_prepare_claim_builder_quote_preserves_head_and_tail_for_long_quotes() -> None:
    quote = ("HEAD " * 500) + ("MIDDLE " * 500) + ("TAIL " * 500)

    prepared = _prepare_claim_builder_quote(quote)

    assert prepared.startswith("HEAD ")
    assert "middle omitted for prompt budget" in prepared
    assert prepared.endswith("TAIL ")


@pytest.mark.asyncio
async def test_partition_claim_ids_for_targeted_rebuild_drops_all_claims_from_targeted_branch_metadata() -> None:
    class _FakeResult:
        def all(self):
            return [
                (
                    uuid.UUID("11111111-1111-1111-1111-111111111111"),
                    encode_claim_notes_metadata(branch_id="b1", question_ids=["b1.1"]),
                ),
                (
                    uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    encode_claim_notes_metadata(branch_id="b6", question_ids=["b6.1"]),
                ),
                (
                    uuid.UUID("33333333-3333-3333-3333-333333333333"),
                    encode_claim_notes_metadata(branch_id="b6", question_ids=["b6.2"]),
                ),
            ]

    class _FakeDB:
        async def execute(self, *args, **kwargs):
            return _FakeResult()

    impacted, preserved = await _partition_claim_ids_for_targeted_rebuild(
        db=_FakeDB(),
        claim_ids=[
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ],
        targeted_branch_ids=["b6"],
        explicit_claim_ids=["22222222-2222-2222-2222-222222222222"],
    )

    assert impacted == [
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    assert preserved == ["11111111-1111-1111-1111-111111111111"]


def test_claim_notes_metadata_roundtrip_and_editor_notes_extraction() -> None:
    encoded = encode_claim_notes_metadata(branch_id="b6", question_ids=["b6.2", "b6.1"])
    decoded = decode_claim_notes_metadata(encoded)

    assert decoded["builder_branch_id"] == "b6"
    assert decoded["builder_question_ids"] == ["b6.1", "b6.2"]
    assert extract_editor_claim_notes(encoded) == ""
    assert extract_editor_claim_notes("Human note") == "Human note"


def test_select_claim_assignment_prefers_semantic_direct_question_context() -> None:
    evidence_id = str(uuid.uuid4())
    selected_branch_id, selected_question_ids = _select_claim_assignment(
        statement="HKMA undertakes to buy US dollars at HK$7.75 under the convertibility undertaking.",
        support_ids={evidence_id},
        branch_id="b3",
        default_question_ids=["b3"],
        batch_evidence_index={
            evidence_id: {
                "lineage": {
                    "question_contexts": [
                        {
                            "branch_id": "b3",
                            "question_id": "b3",
                            "question": "How did the peg change from silver to sterling to USD?",
                            "direct_or_contextual": "contextual",
                        },
                        {
                            "branch_id": "b4",
                            "question_id": "b4.1",
                            "question": "How does the 7.75 / 7.85 convertibility corridor work?",
                            "direct_or_contextual": "direct",
                        },
                    ]
                }
            }
        },
    )

    assert selected_branch_id == "b4"
    assert selected_question_ids == ["b4.1", "b3"]


class _FakePersistDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_calls = 0

    def add(self, item: object) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


@pytest.mark.asyncio
async def test_persist_claim_batch_reuses_previous_verification_state_and_semantic_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.publish_event",
        lambda *args, **kwargs: None,
    )
    db = _FakePersistDB()
    all_claim_ids: list[str] = []
    evidence_id = str(uuid.uuid4())
    signature = _claim_rebuild_signature(
        statement="HKMA undertakes to buy US dollars at HK$7.75 under the convertibility undertaking.",
        claim_type="fact",
        support_ids={evidence_id},
        illustrative_ids=set(),
    )

    saved_claims, stats = await _persist_claim_batch(
        db=db,
        project_id=str(uuid.uuid4()),
        claims_data=[
            {
                "claim_type": "FACT",
                "statement": "HKMA undertakes to buy US dollars at HK$7.75 under the convertibility undertaking.",
                "supporting_evidence_ids": [evidence_id],
                "illustrative_evidence_ids": [],
                "attribution": None,
            }
        ],
        batch_ids={evidence_id},
        branch_id="b3",
        branch_question_ids=["b3"],
        batch_evidence_index={
            evidence_id: {
                "lineage": {
                    "question_contexts": [
                        {
                            "branch_id": "b3",
                            "question_id": "b3",
                            "question": "How did the peg change from silver to sterling to USD?",
                            "direct_or_contextual": "contextual",
                        },
                        {
                            "branch_id": "b4",
                            "question_id": "b4.1",
                            "question": "How does the 7.75 / 7.85 convertibility corridor work?",
                            "direct_or_contextual": "direct",
                        },
                    ]
                }
            }
        },
        previous_rebuild_lookup={
            signature: [
                {
                    "claim_id": str(uuid.uuid4()),
                    "verification_level": VerificationLevel.OBSERVED,
                    "verification_fingerprint": "fp-same",
                    "was_gap_claim": False,
                }
            ]
        },
        run_id=str(uuid.uuid4()),
        all_claim_ids=all_claim_ids,
    )

    created_claims = [item for item in db.added if isinstance(item, Claim)]
    created_links = [item for item in db.added if isinstance(item, ClaimEvidence)]

    assert len(saved_claims) == 1
    assert len(created_claims) == 1
    assert len(created_links) == 1
    assert stats["reused_verification_state_count"] == 1
    assert created_claims[0].verification_level == VerificationLevel.OBSERVED
    assert created_claims[0].verification_fingerprint == "fp-same"
    assert decode_claim_notes_metadata(created_claims[0].notes)["builder_branch_id"] == "b4"
    assert decode_claim_notes_metadata(created_claims[0].notes)["builder_question_ids"] == ["b3", "b4.1"]
    assert all_claim_ids == [saved_claims[0]["claim_id"]]


@pytest.mark.asyncio
async def test_persist_claim_batch_does_not_reuse_verification_state_for_non_lead_gap_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.publish_event",
        lambda *args, **kwargs: None,
    )
    db = _FakePersistDB()
    all_claim_ids: list[str] = []
    evidence_id = str(uuid.uuid4())
    signature = _claim_rebuild_signature(
        statement="The Exchange Fund constrains discretionary monetary policy under the linked exchange rate system.",
        claim_type="fact",
        support_ids={evidence_id},
        illustrative_ids=set(),
    )

    await _persist_claim_batch(
        db=db,
        project_id=str(uuid.uuid4()),
        claims_data=[
            {
                "claim_type": "FACT",
                "statement": "The Exchange Fund constrains discretionary monetary policy under the linked exchange rate system.",
                "supporting_evidence_ids": [evidence_id],
                "illustrative_evidence_ids": [],
                "attribution": None,
            }
        ],
        batch_ids={evidence_id},
        branch_id="b6",
        branch_question_ids=["b6.3"],
        batch_evidence_index={
            evidence_id: {
                "lineage": {"question_contexts": []}
            }
        },
        previous_rebuild_lookup={
            signature: [
                {
                    "claim_id": "c2",
                    "verification_level": VerificationLevel.OBSERVED,
                    "verification_fingerprint": "fp-gap",
                    "was_gap_claim": True,
                }
            ]
        },
        run_id=str(uuid.uuid4()),
        all_claim_ids=all_claim_ids,
    )

    created_claim = next(item for item in db.added if isinstance(item, Claim))
    assert created_claim.verification_fingerprint is None


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self) -> None:
        self.deleted_claim_ids: list[str] = []
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        return _FakeScalarResult(None)

    async def commit(self) -> None:
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_build_claims_targeted_rebuild_preserves_unaffected_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    processed_branch_ids: list[str] = []
    deleted_claim_ids: list[str] = []

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.async_session_factory",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.update_generation_progress",
        lambda *args, **kwargs: None,
    )

    async def _fake_load_branch_batches(**kwargs):
        return (
            {"e1": {"id": "e1"}, "e2": {"id": "e2"}},
            [
                ("b1", [{"id": "e1", "lineage": {"question_contexts": []}, "source": {}, "contextual_asset_types": [], "verbatim_quote": "A", "asset_type": "timeline"}]),
                ("b6", [{"id": "e2", "lineage": {"question_contexts": []}, "source": {}, "contextual_asset_types": [], "verbatim_quote": "B", "asset_type": "contrast"}]),
            ],
        )

    async def _fake_partition(**kwargs):
        return (["claim-drop"], ["claim-keep"])

    async def _fake_delete_claims(db, claim_ids):
        deleted_claim_ids.extend(claim_ids)

    def _fake_split(batch):
        return [batch]

    async def _fake_call_sonnet_structured(**kwargs):
        payload = __import__("json").loads(kwargs["prompt"])
        processed_branch_ids.append(payload["branch_id"])
        return '[{"claim_type":"FACT","statement":"Rebuilt claim","supporting_evidence_ids":["e2"],"illustrative_evidence_ids":[],"attribution":null}]'

    async def _fake_persist_claim_batch(**kwargs):
        kwargs["all_claim_ids"].append("claim-new")
        return (
            [{
                "claim_id": "claim-new",
                "claim_type": "fact",
                "statement": "Rebuilt claim",
                "supporting_evidence_ids": ["e2"],
                "illustrative_evidence_ids": [],
            }],
            {"skipped_no_support_count": 0, "skipped_empty_statement_count": 0},
        )

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._load_branch_batches",
        _fake_load_branch_batches,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._partition_claim_ids_for_targeted_rebuild",
        _fake_partition,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._delete_claims",
        _fake_delete_claims,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._split_evidence_batch",
        _fake_split,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._persist_claim_batch",
        _fake_persist_claim_batch,
    )

    result = await build_claims(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "language": "ru",
            "manifest_id": "",
            "evidence_ids": ["e1", "e2"],
            "claim_ids": ["claim-keep", "claim-drop"],
            "iteration_counts": {"verify_research_loops": 1},
            "research_gaps": [{"branch_id": "b6", "claim_id": "claim-drop"}],
        }
    )

    assert deleted_claim_ids == ["claim-drop"]
    assert processed_branch_ids == ["b6"]
    assert result["claim_ids"] == ["claim-keep", "claim-new"]
    assert result["rebuilt_claim_ids"] == ["claim-new"]
    assert result["preserved_claim_ids"] == ["claim-keep"]
    assert result["targeted_branch_ids"] == ["b6"]


@pytest.mark.asyncio
async def test_build_claims_passes_full_merged_gap_claim_ids_to_previous_rebuild_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    captured_previous_gap_claim_ids: list[str] = []

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.async_session_factory",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.update_generation_progress",
        lambda *args, **kwargs: None,
    )

    async def _fake_load_branch_batches(**kwargs):
        return (
            {"e1": {"id": "e1"}},
            [
                ("b6", [{"id": "e1", "lineage": {"question_contexts": []}, "source": {}, "contextual_asset_types": [], "verbatim_quote": "A", "asset_type": "contrast"}]),
            ],
        )

    async def _fake_partition(**kwargs):
        return (["claim-drop"], [])

    async def _fake_load_previous_claim_rebuild_state(**kwargs):
        captured_previous_gap_claim_ids.extend(sorted(kwargs["previous_gap_claim_ids"]))
        return {}

    async def _fake_delete_claims(db, claim_ids):
        return None

    def _fake_split(batch):
        return []

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._load_branch_batches",
        _fake_load_branch_batches,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._partition_claim_ids_for_targeted_rebuild",
        _fake_partition,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._load_previous_claim_rebuild_state",
        _fake_load_previous_claim_rebuild_state,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._delete_claims",
        _fake_delete_claims,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._split_evidence_batch",
        _fake_split,
    )

    await build_claims(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "language": "ru",
            "manifest_id": "",
            "evidence_ids": ["e1"],
            "claim_ids": ["claim-drop"],
            "iteration_counts": {"verify_research_loops": 1},
            "research_gaps": [{"branch_id": "b6", "claim_id": "c1", "claim_ids": ["c1", "c2"]}],
        }
    )

    assert set(captured_previous_gap_claim_ids) == {"c1", "c2"}


@pytest.mark.asyncio
async def test_build_claims_shortens_evidence_ids_for_llm_and_restores_before_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    evidence_id_1 = str(uuid.uuid4())
    evidence_id_2 = str(uuid.uuid4())
    captured_prompt_evidence_ids: list[str] = []
    captured_claims_data: list[dict] = []

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.async_session_factory",
        lambda: session,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.publish_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.update_generation_progress",
        lambda *args, **kwargs: None,
    )

    async def _fake_load_branch_batches(**kwargs):
        return (
            {
                evidence_id_1: {"id": evidence_id_1},
                evidence_id_2: {"id": evidence_id_2},
            },
            [
                (
                    "b1",
                    [
                        {
                            "id": evidence_id_1,
                            "lineage": {"question_contexts": []},
                            "source": {},
                            "contextual_asset_types": [],
                            "verbatim_quote": "A",
                            "asset_type": "timeline",
                        },
                        {
                            "id": evidence_id_2,
                            "lineage": {"question_contexts": []},
                            "source": {},
                            "contextual_asset_types": [],
                            "verbatim_quote": "B",
                            "asset_type": "case",
                        },
                    ],
                ),
            ],
        )

    async def _fake_call_sonnet_structured(**kwargs):
        payload = __import__("json").loads(kwargs["prompt"])
        captured_prompt_evidence_ids.extend(
            item["id"] for item in payload["evidence_spans"]
        )
        return __import__("json").dumps(
            [
                {
                    "claim_type": "FACT",
                    "statement": "Restored evidence references.",
                    "supporting_evidence_ids": [payload["evidence_spans"][0]["id"]],
                    "illustrative_evidence_ids": [payload["evidence_spans"][1]["id"]],
                    "attribution": None,
                }
            ]
        )

    async def _fake_persist_claim_batch(**kwargs):
        captured_claims_data.extend(kwargs["claims_data"])
        kwargs["all_claim_ids"].append("claim-new")
        return (
            [
                {
                    "claim_id": "claim-new",
                    "claim_type": "fact",
                    "statement": "Restored evidence references.",
                    "supporting_evidence_ids": [evidence_id_1],
                    "illustrative_evidence_ids": [evidence_id_2],
                }
            ],
            {"skipped_no_support_count": 0, "skipped_empty_statement_count": 0},
        )

    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._load_branch_batches",
        _fake_load_branch_batches,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder._persist_claim_batch",
        _fake_persist_claim_batch,
    )

    await build_claims(
        {
            "generation_run_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "language": "ru",
            "manifest_id": "",
            "evidence_ids": [evidence_id_1, evidence_id_2],
            "claim_ids": [],
        }
    )

    assert set(captured_prompt_evidence_ids) == {"e1", "e2"}
    assert captured_claims_data == [
        {
            "claim_type": "FACT",
            "statement": "Restored evidence references.",
            "supporting_evidence_ids": [evidence_id_1],
            "illustrative_evidence_ids": [evidence_id_2],
            "attribution": None,
        }
    ]
