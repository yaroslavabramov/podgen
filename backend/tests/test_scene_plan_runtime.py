"""Real PostgreSQL import, clearance, fact-check preview and resume; the provider is local."""

import json
from copy import deepcopy

from podgen import writing_cli
from podgen.research_writing_cli import main as research_main
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.models.contracts import WireReply
from podgen.writing.planning_choice import KEY, SCENE_VERSION

from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_scene_plan import scene_proposal


class SceneProvider(ReadingProvider):
    def submit(self, body, *, client_request_id):
        if body["metadata"]["podgen_stage"] != "plan":
            return super().submit(body, client_request_id=client_request_id)
        self.requests.append(deepcopy(body))
        visible = payload(body)
        self.plan = scene_proposal(
            visible["material_catalog"], [s["label"] for s in visible["assignment"]["speakers"]]
        )
        return WireReply(
            json.dumps(
                dict(
                    id="resp_scene",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(
                            type="message",
                            role="assistant",
                            content=[dict(type="output_text", text=json.dumps(self.plan))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                )
            ).encode()
        )


def test_candidate_clearance_writing_and_replayed_fact_wire(own_dataset, tmp_path, monkeypatch):
    prefix, _, options, _, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    options.update(behavior_profile="editorial-candidate/1", review_mode="independent-readings/1")
    repo, project, *_ = own_dataset
    for key, model, roles in (
        ("planner_profile", "gpt-6.1-sol", ["planner"]),
        ("plan_reviewer_profile", "gpt-6.1-terra", ["plan_reviewer"]),
        ("writer_profile", "claude-opus-5-5", ["writer"]),
        ("reviewer_profile", "gpt-6.1-terra", ["editorial_reviewer"]),
    ):
        profile = create_profile(
            repo,
            project,
            model=model,
            roles=roles,
            context_limit=1050000,
            output_limit=24576,
            reasoning_effort="medium",
        )
        options[key] = profile.ref.model_dump_json()
    provider = SceneProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **kw: provider)
    args = [*prefix, "run", "--stop-after-plan"]
    for k, v in options.items():
        args.extend(["--" + k.replace("_", "-"), str(v)])
    assert research_main(args) == 0

    def stages():
        return [body["metadata"]["podgen_stage"] for body in provider.requests]

    assert stages() == ["plan", "plan-review"]
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    assert journal.manifest(options["run_id"])[KEY] == SCENE_VERSION
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "complete") == 0
    assert stages() == ["plan", "plan-review", "draft", "reader-review", "fact-review"]
    wires = {body["metadata"]["podgen_stage"]: body for body in provider.requests}
    scenes = payload(wires["draft"])["story"]
    assert scenes["ending"]["development"] == provider.plan["plan"]["ending"]["development"]
    assert scenes["ending"]["support_refs"] == ["M3"]
    assert provider.plan["editorial_notes"][0]["note"] not in repr(wires["draft"])
    assert provider.plan["editorial_notes"][0]["note"] in repr(wires["plan-review"])
    fact = payload(wires["fact-review"])
    assert (
        fact["factual_remit"]["claims_to_check"][0]["claim_scope"]
        == provider.plan["editorial_notes"][1]["note"]
    )
    assert "claims_to_check" not in payload(wires["reader-review"])
    draft = journal.output(journal.invocation(options["run_id"], "draft"))["output"]
    file = tmp_path / "draft.json"
    file.write_text(json.dumps(draft))
    view = tmp_path / "fact-preview.json"
    assert (
        invoke(
            prefix,
            "preview",
            run_id=options["run_id"],
            case="snow-pump",
            shared_ref="@" + str(tmp_path / "shared-ref.json"),
            profile=options["reviewer_profile"],
            stage="fact-review",
            draft=file,
            out=view,
        )
        == 0
    )
    assert json.loads(view.read_text())["request"] == wires["fact-review"]
