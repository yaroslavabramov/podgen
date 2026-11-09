"""Only three released system instructions change; schema, payload, model policy and history do not."""

from copy import copy
from dataclasses import fields
from pathlib import Path
from uuid import uuid4

from podgen.runtime.evaluation.preview import preview_request
from podgen.writing import prompts
from podgen.writing.behavior import DEFAULT, NAMES, BehaviorProfile, resolve_behavior

from tests.test_v5_role_wire import fixture


def test_candidate_2_is_only_three_owned_instruction_changes_with_exact_format_and_revision_assembly():
    root = Path(prompts.__file__).parent / "candidate"
    components = {
        name: (root / name).read_bytes().decode() for name in ("plan.md", "plan-review.md", "plan-revise.md")
    }
    assert "editorial-candidate/2" in NAMES
    assert resolve_behavior().name == DEFAULT == "production/1"
    changed = {"plan", "plan-review", "plan-revise"}
    marker = "Read the supplied `candidate_plan`"
    for editorial_pass in (1, 2, 3):
        before = resolve_behavior("editorial-candidate/1", editorial_pass)
        after = resolve_behavior("editorial-candidate/2", editorial_pass)
        for field in fields(BehaviorProfile):
            if field.name not in ("name", "resources"):
                assert getattr(before, field.name) == getattr(after, field.name)
        assert before.verification == after.verification
        assert before.craft("ru") == after.craft("ru")
        assert before.resources["readings"] == after.resources["readings"]
        assert before.resources["repairs"] == after.resources["repairs"]
        for fmt in ("solo", "dialogue"):
            roles = before.resources["formats"][fmt]
            assert {
                role for role in roles if before.instructions(role, fmt) != after.instructions(role, fmt)
            } == changed
            prefix, boundary, _ = (
                resolve_behavior("narrative-rebuild/1")
                .instructions("plan-review", fmt)
                .body.partition(marker)
            )
            assert boundary and prefix
            own = dict(components)
            own["plan-review.md"] = prefix + components["plan-review.md"]
            for role in changed:
                assert after.instructions(role, fmt) == prompts._assemble(role, fmt, own)
            reviewer = after.instructions("plan-review", fmt).body
            assert reviewer.startswith(prefix) and reviewer.count(prefix) == 1
            assert reviewer == prefix + components["plan-review.md"]
            assert (
                after.instructions("plan-revise", fmt).body
                == components["plan.md"] + "\n\n" + components["plan-revise.md"]
            )

    materials, _, candidate, profile, script, reader, fact, review = fixture()
    run_id = uuid4()
    values = {
        "plan": {},
        "plan-review": dict(candidate=candidate),
        "plan-review-revised": dict(candidate=candidate),
        "plan-revise": dict(candidate=candidate, plan_review=review),
        "draft": dict(approved_plan=candidate),
        "reader-review": dict(draft=script),
        "fact-review": dict(draft=script),
        "revise-1": dict(draft=script, approved_plan=candidate, reader_feedback=reader, fact_feedback=fact),
    }
    for stage, inputs in values.items():
        current_profile = copy(profile)
        author = stage == "draft" or stage.startswith("revise-")
        current_profile.provider = "anthropic" if author else "openai"
        current_profile.api_family = "messages" if author else "responses"
        current_profile.adapter_version = "anthropic-messages-v1" if author else "openai-responses-v1"
        current_profile.model_revision_or_alias = (
            "claude-opus-5-5"
            if author
            else "gpt-6.1-sol"
            if stage in ("plan", "plan-revise")
            else "gpt-6.1-terra"
        )
        current_profile.context_limit = 200000
        current_profile.output_limit = 24576
        current_profile.generation_parameters = dict(
            reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
        )
        old = preview_request(
            run_id, stage, current_profile, materials, behavior_profile="editorial-candidate/1", **inputs
        )
        new = preview_request(
            run_id, stage, current_profile, materials, behavior_profile="editorial-candidate/2", **inputs
        )
        if stage.startswith("plan"):
            assert old["instructions"] != new["instructions"]
        else:
            assert old["instructions"] == new["instructions"]
        assert {k: v for k, v in old.items() if k != "instructions"} == {
            k: v for k, v in new.items() if k != "instructions"
        }
