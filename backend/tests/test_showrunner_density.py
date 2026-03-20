from app.models.claim import VerificationLevel
from app.pipeline.phase5_planning.showrunner import (
    SHOWRUNNER_SYSTEM,
    _cap_act_claim_density,
    _normalize_plan,
)


def test_cap_act_claim_density_keeps_high_priority_claims() -> None:
    claims_summary = []
    for idx in range(18):
        claims_summary.append(
            {
                "id": f"c{idx}",
                "verification": VerificationLevel.VERIFIED if idx < 8 else VerificationLevel.OBSERVED,
                "source_diversity": 2 if idx < 8 else 1,
                "direct_support_count": 1 if idx < 8 else 0,
                "has_illustrative": idx % 3 == 0,
                "type": "fact",
            }
        )

    plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "title": "Act 1",
                "estimated_duration_seconds": 360,
                "claim_ids": [claim["id"] for claim in claims_summary],
                "conflict_brief_ids": [],
                "content_quotas": {},
            }
        ],
    }

    capped = _cap_act_claim_density(plan, claims_summary)

    assert len(capped["acts"][0]["claim_ids"]) == 8
    assert set(capped["acts"][0]["claim_ids"]) <= {f"c{idx}" for idx in range(8)}


def test_cap_act_claim_density_preserves_disputed_and_conflict_claims() -> None:
    claims_summary = []
    for idx in range(8):
        claims_summary.append(
            {
                "id": f"v{idx}",
                "verification": VerificationLevel.VERIFIED,
                "source_diversity": 2,
                "direct_support_count": 1,
                "has_illustrative": False,
                "type": "fact",
            }
        )
    claims_summary.append(
        {
            "id": "d1",
            "verification": VerificationLevel.DISPUTED,
            "source_diversity": 1,
            "direct_support_count": 0,
            "has_illustrative": False,
            "type": "fact",
        }
    )

    plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "title": "Act 1",
                "estimated_duration_seconds": 360,
                "claim_ids": [claim["id"] for claim in claims_summary],
                "conflict_brief_ids": [],
                "content_quotas": {},
            }
        ],
    }

    capped = _cap_act_claim_density(
        plan,
        claims_summary,
        preserved_claim_ids={"d1"},
        disputed_claim_ids={"d1"},
        conflict_claim_ids=set(),
    )

    assert "d1" in capped["acts"][0]["claim_ids"]
    assert len(capped["acts"][0]["claim_ids"]) == 8


def test_cap_act_claim_density_still_honors_cap_when_preserved_claims_exceed_limit() -> None:
    claims_summary = [
        {
            "id": f"d{idx}",
            "verification": VerificationLevel.DISPUTED,
            "source_diversity": 1,
            "direct_support_count": 0,
            "has_illustrative": False,
            "type": "fact",
        }
        for idx in range(9)
    ]

    plan = {
        "act_count": 1,
        "acts": [
            {
                "act_number": 1,
                "title": "Act 1",
                "estimated_duration_seconds": 360,
                "claim_ids": [claim["id"] for claim in claims_summary],
                "conflict_brief_ids": [],
                "content_quotas": {},
            }
        ],
    }

    capped = _cap_act_claim_density(
        plan,
        claims_summary,
        preserved_claim_ids={claim["id"] for claim in claims_summary},
        disputed_claim_ids={claim["id"] for claim in claims_summary},
        conflict_claim_ids=set(),
    )

    assert len(capped["acts"][0]["claim_ids"]) == 8


def test_normalize_plan_preserves_story_fields() -> None:
    plan = _normalize_plan(
        {
            "acts": [
                {
                    "act_number": 1,
                    "story_role": "setup",
                    "title": "How the money got weird",
                    "purpose": "Establish the strange starting point and why the listener should care.",
                    "opening_question": "Why would a city let private banks print money?",
                    "ending_turn": "The historical oddity becomes an institutional design puzzle.",
                    "focus_branch_ids": ["b1", "b2", "b1"],
                    "estimated_duration_seconds": 420,
                    "claim_ids": [],
                    "conflict_brief_ids": [],
                    "content_quotas": {},
                }
            ]
        },
        duration_target=1800,
    )

    act = plan["acts"][0]
    assert act["story_role"] == "setup"
    assert act["purpose"].startswith("Establish the strange starting point")
    assert act["opening_question"] == "Why would a city let private banks print money?"
    assert act["ending_turn"] == "The historical oddity becomes an institutional design puzzle."
    assert act["focus_branch_ids"] == ["b1", "b2"]


def test_showrunner_prompt_does_not_hardcode_three_acts() -> None:
    assert '"act_count": 3' not in SHOWRUNNER_SYSTEM
    assert "compress this to 3 acts" not in SHOWRUNNER_SYSTEM
    assert "do not default to 3 acts" in SHOWRUNNER_SYSTEM
