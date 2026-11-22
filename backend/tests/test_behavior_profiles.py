"""Full native replay with real persisted inputs and archived provider answers. No external calls."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_documents import import_document
from podgen.persistence.editorial_inputs import import_shared, load_editorial_materials
from podgen.runtime.evaluation.contracts import RunLimits, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.evaluation.reusable_plan import export_plan_ref
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from podgen.writing.behavior import DEFAULT, resolve_behavior

ROOT = Path(__file__).resolve().parents[2] / "evals"
HISTORY = ROOT / "reports/2026-11-08-snow-narrative-rebuild"


def read(path):
    return json.loads(path.read_text())


def archived_input(repo, project):
    source = ROOT / "editorial-inputs/current/snow-pump/history/before-percolation-2026-11-08.json"
    packet = read(source)
    refs = {}
    for spec in packet["documents"]:
        path = spec["source_path"]
        src = next(p / path for p in (ROOT, ROOT / "cases/development/wp02-batch-01") if (p / path).is_file())
        ref = import_document(
            repo, project, src.read_bytes(), source_path=path, purpose=spec["purpose"], scope_key="snow"
        )
        refs[path] = ref.model_dump(mode="json")
    return import_shared(repo, project, packet, refs, original_bytes=source.read_bytes())


class ArchiveProvider:
    def __init__(self):
        self.calls = []
        for path in sorted((HISTORY / "calls").glob("*/response.json")):
            saved = read(path)
            if saved.get("id"):
                self.calls.append((read(path.with_name("podgen-request.json")), saved))
        self.completed = 0

    def count(self, body):
        return WireReply(b'{"input_tokens":10000}')

    def submit(self, body, *, client_request_id):
        expected, answer = self.calls[self.completed]
        # The journal allocates real invocation and run IDs. Nothing behavioral may differ.
        assert body["metadata"]["podgen_stage"] == expected["metadata"]["podgen_stage"]
        assert {k: v for k, v in body.items() if k != "metadata"} == {
            k: v for k, v in expected.items() if k != "metadata"
        }
        self.completed += 1
        if "content" in answer:
            output = [
                dict(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        dict(type="output_text", text=block["text"])
                        for block in answer["content"]
                        if block["type"] == "text"
                    ],
                )
            ]
        else:
            output = answer["output"]
        receipt = dict(
            id=("resp_" + answer["id"]) if "content" in answer else answer["id"],
            model=body["model"],
            metadata=body["metadata"],
            status="completed",
            output=output,
            usage={"input_tokens": 10000, "output_tokens": answer["usage"]["output_tokens"]},
        )
        return WireReply(json.dumps(receipt).encode())


def test_recreate_three_historical_readings_with_identical_requests_and_final_text(repo, scope):
    project = scope[0]
    journal = EvaluationJournal(repo, project)
    shared = archived_input(repo, project)

    def profile(model, roles):
        return create_profile(
            repo,
            project,
            model=model,
            roles=roles,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
            verbosity="high",
        ).ref

    writer = profile("claude-opus-5-5", ["writer"])
    reviewer = profile("gpt-6.1-terra", ["editorial_reviewer", "plan_reviewer"])
    planner = profile("gpt-6.1-sol", ["planner"])
    provider = ArchiveProvider()
    app = WritingEvaluation(repo, lambda _: provider)
    common = dict(
        case_id="snow-pump",
        project_id=project,
        actor_id=repo.actor_id,
        author_ref=shared,
        writer_profile_ref=writer,
        reviewer_profile_ref=reviewer,
        behavior_profile="narrative-rebuild/1",
        review_anchor_policy="bounded-selection-v1",
        limits=RunLimits(
            max_total_input_tokens=600000, max_total_output_tokens=300000, max_elapsed_seconds=14400
        ),
    )
    plan_ref = script = None
    for index, limit in enumerate([2, 1, 2], 1):
        rid = uuid4()
        kwargs = (
            dict(plan_mode="own-plan", planner_profile_ref=planner, plan_reviewer_profile_ref=reviewer)
            if index == 1
            else dict(plan_mode="fixed-plan", fixed_plan_input_ref=plan_ref, initial_script_ref=script)
        )
        command = WritingRunCommand(
            **common, **kwargs, run_id=rid, editorial_pass=index, max_script_revisions=limit
        )
        app.run(command)
        outcome = read_outcome(journal, rid)
        assert outcome is not None, journal.run(rid)
        script = RuntimeRef.model_validate(outcome["script_ref"])
        original = ("first-cycle", "second-cycle", "final-reading")[index - 1]
        original_result = read(HISTORY / original / "review-outcome.json")
        assert outcome["script_hash"] == original_result["script_hash"] == digest(outcome["output"])
        manifest = journal.manifest(rid)
        assert manifest["behavior_profile"] == "narrative-rebuild/1" and manifest["editorial_pass"] == index
        # New admissions hash their actual complete text; saved historical request
        # bodies above remain byte-identical, including the older framed ID era.
        from hashlib import sha256

        from podgen.domain.contracts.common import ArtifactRef

        for stage, version in manifest["role_instructions_versions"].items():
            saved = repo.get(project, ArtifactRef.model_validate(manifest["prompt_refs"][stage]))
            assert version.endswith(sha256(saved.payload["template"].encode()).hexdigest())
        if index == 1:
            plan_ref = export_plan_ref(journal, rid)
            with pytest.raises(ContractError, match="expected_planning_clearance"):
                load_editorial_materials(
                    app.generation, project, shared, case_id="snow-pump", fixed_ref=script
                )
    assert provider.completed == len(provider.calls) == 27


def test_default_and_named_profile_use_immutable_resources(monkeypatch):
    from podgen.writing import prompts

    def changed(*_, **__):
        raise AssertionError("a released profile consulted editable components")

    monkeypatch.setattr(prompts, "load_instruction_set", changed)
    monkeypatch.setattr(prompts, "load_reading_instructions", changed)
    assert resolve_behavior().name == DEFAULT == "production/1"
    assert resolve_behavior().instructions("draft", "solo").body
    assert resolve_behavior("narrative-rebuild/1", 1).instructions(
        "reader-review", "solo"
    ) != resolve_behavior("narrative-rebuild/1", 3).instructions("reader-review", "solo")
    with pytest.raises(ContractError):
        resolve_behavior("existing/1", 3)
