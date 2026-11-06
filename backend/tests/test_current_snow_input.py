"""Current Snow through source admission and real role compilation; no model quality claims."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.evaluation.wire_budget import check_visible
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projections import CONTRACT
from tests.independent_planner_provider import IndependentPlannerProvider
from tests.test_editorial_independence import invoke, payload

ROOT = Path(__file__).resolve().parents[2]
CURRENT = ROOT / "evals/editorial-inputs/current/snow-pump"
CORPUS = ROOT / "evals/cases/development/wp02-batch-01"


def source_file(document):
    base = ROOT / "evals" if document["source_path"].startswith("editorial-inputs/") else CORPUS
    return base / document["source_path"]


def test_current_snow_sources_and_exact_excerpt_ranges():
    current = EditorialInput.model_validate_json((CURRENT / "shared.json").read_bytes())
    texts = {}
    for document in current.model_dump(mode="json")["documents"]:
        raw = source_file(document).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == document["sha256"]
        texts[document["key"]] = raw.decode()
    for card in current.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            assert texts[excerpt.document][excerpt.start : excerpt.end] == excerpt.text


def test_current_snow_informed_cli_and_every_role_receive_the_assignment(repo, scope, tmp_path, monkeypatch):
    project, _ = scope
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    data = json.loads((CURRENT / "shared.json").read_text())
    mapping = {}
    for document in data["documents"]:
        output = tmp_path / (document["key"] + "-ref.json")
        assert (
            invoke(
                prefix,
                "editorial-source-import",
                file=source_file(document),
                source_path=document["source_path"],
                purpose=document["purpose"],
                scope="current-snow-test",
                out=output,
            )
            == 0
        )
        mapping[document["source_path"]] = json.loads(output.read_text())
    mapping_path = tmp_path / "source-map.json"
    mapping_path.write_text(json.dumps(mapping))
    shared_path = tmp_path / "shared-ref.json"
    assert (
        invoke(
            prefix,
            "editorial-import",
            input=CURRENT / "shared.json",
            source_map=mapping_path,
            out=shared_path,
        )
        == 0
    )
    shared = RuntimeRef.model_validate_json(shared_path.read_bytes())
    generation = generation_repo(repo)
    materials = load_editorial_materials(generation, project, shared, case_id="snow-pump")
    assert materials.packet == EditorialInput.model_validate(data)
    assert materials.fixed is None
    assert len(materials.documents) == len(data["documents"])
    profile = create_profile(
        repo,
        project,
        model="local-double",
        context_limit=1050000,
        output_limit=32768,
        roles=["planner", "plan_reviewer", "writer", "editorial_reviewer"],
    )
    profile_ref = json.dumps(profile.ref.model_dump(mode="json"))
    provider = IndependentPlannerProvider(review_decision="revise")
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    assert (
        invoke(
            prefix,
            "run",
            review_mode="informed-v1",
            run_id=uuid4(),
            case="snow-pump",
            author_ref="@" + str(shared_path),
            plan_mode="own-plan",
            planner_profile=profile_ref,
            plan_reviewer_profile=profile_ref,
            writer_profile=profile_ref,
            reviewer_profile=profile_ref,
            export_dir=tmp_path / "run",
        )
        == 0
    )
    assert [body["metadata"]["podgen_stage"] for body in provider.requests] == [
        "plan",
        "plan-review",
        "draft",
        "review",
        "revise",
    ]
    expected_assignment = {
        key: value
        for key, value in data["assignment"].items()
        if key != "key" and (key not in ("excluded_topics", "style_guidance") or value)
    }
    for body in provider.requests:
        assert payload(body)["assignment"] == expected_assignment
    assert payload(provider.requests[0])["dossier"]["threads"] == []
    assert "candidate_plan" not in payload(provider.requests[0])
    assert payload(provider.requests[2])["story"]["opening"] == {
        key: value for key, value in provider.plan["plan"]["opening"].items() if key != "material_refs"
    }
    assert (tmp_path / "run/editorial-input-original.json").read_bytes() == (
        CURRENT / "shared.json"
    ).read_bytes()
    plan_review = dict(
        decision="revise",
        decision_reason="Synthetic transport probe.",
        listener_answer="Probe.",
        important_limits=[],
        strengths=[],
        research_requests=[],
        duration_note="Not measured.",
        findings=[
            dict(
                targets=["/plan/opening/approach"],
                priority="important",
                observation="Probe.",
                requested_change="Synthetic repair.",
                preserve=[],
                material_refs=[],
            )
        ],
    )
    inputs = {
        "plan": {},
        "plan-review": dict(candidate=provider.plan),
        "plan-revise": dict(candidate=provider.plan, plan_review=plan_review),
        "plan-review-revised": dict(candidate=provider.plan),
        "draft": dict(approved_plan=provider.plan),
        "review": dict(approved_plan=provider.plan, draft=provider.script),
        "revise": dict(approved_plan=provider.plan, draft=provider.script, feedback=provider.feedback),
    }
    parsed_profile = load_profile(generation, project, profile.ref, "plan")
    for stage, values in inputs.items():
        body = preview_request(uuid4(), stage, parsed_profile, materials, **values)
        assert payload(body)["assignment"] == expected_assignment
        assert payload(body)["output_language"] == data["assignment"]["output_language"]
        assert "# " + data["assignment"]["format"].title() in body["instructions"]
        check_visible(body, CONTRACT)
    for stage in ("reader-review", "fact-review"):
        body = preview_request(uuid4(), stage, parsed_profile, materials, draft=provider.script)
        visible = payload(body)
        assert visible["script"]["utterances"][0]["text"] == provider.script["utterances"][0]["text"]
        assert "assignment" not in visible
        if stage == "reader-review":
            assert set(visible) == {"script", "audience", "genre", "format", "output_language"}
            assert visible["audience"] == data["assignment"]["listener_profile"]
            assert visible["genre"] == data["assignment"]["genre"]
        else:
            remit = visible["factual_remit"]
            assert remit["question"] == data["assignment"]["question"]
            assert remit["required_topics"] == data["assignment"]["required_topics"]
            assert [card["claim"] for card in visible["support"]] == [
                card["claim"] for card in data["dossier"]["knowledge_cards"]
            ]
        check_visible(body, CONTRACT)
