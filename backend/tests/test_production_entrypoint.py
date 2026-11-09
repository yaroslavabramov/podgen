"""Cheap production entrypoint, frozen-command and gateway-budget checks. No database or external calls."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits, WritingRunCommand, command_payload
from podgen.runtime.evaluation.input_budget_recovery import effective_limits
from podgen.runtime.evaluation.wire_budget import (
    PRODUCTION_INPUT_BUDGET,
    PRODUCTION_RUN_LIMITS,
    input_budget_for_profile,
    validate_manifest_budget,
)
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.behavior import DEFAULT, PRODUCTION, resolve_behavior
from podgen.writing_cli import parser


def test_normal_cli_uses_production_own_plan_and_keeps_research_options_out_of_run_and_preview():
    project, actor = str(uuid4()), str(uuid4())
    prefix = ["--project", project, "--actor", actor, "--objects", "/tmp/unused-production-test-objects"]
    args = [
        *prefix,
        "run",
        "--run-id",
        str(uuid4()),
        "--case",
        "snow-pump",
        "--author-ref",
        "shared",
        "--writer-profile",
        "writer",
        "--reviewer-profile",
        "reviewer",
        "--planner-profile",
        "planner",
        "--plan-reviewer-profile",
        "plan-reviewer",
        "--export-dir",
        "/tmp/unused-production-test-export",
    ]
    ordinary = parser().parse_args(args)
    assert ordinary.plan_mode == "own-plan" and ordinary.review_mode == "independent-readings/1"
    assert ordinary.max_script_revisions == 2
    assert not hasattr(ordinary, "behavior_profile") and not hasattr(ordinary, "editorial_pass")
    assert {k: getattr(ordinary, k) for k in PRODUCTION_RUN_LIMITS} == PRODUCTION_RUN_LIMITS
    for command in ("run", "preview"):
        sub = next(
            action
            for action in parser()._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        ).choices[command]
        assert "--behavior-profile" not in sub.format_help() and "--editorial-pass" not in sub.format_help()
        if command == "run":
            assert "--review-mode" not in sub.format_help()
    with pytest.raises(SystemExit):
        parser().parse_args([*args, "--behavior-profile", "editorial-candidate/2"])
    research = parser(research=True).parse_args(
        [*args, "--behavior-profile", "narrative-rebuild/1", "--editorial-pass", "1"]
    )
    assert research.behavior_profile == "narrative-rebuild/1" and research.editorial_pass == 1
    assert ordinary.max_total_input_tokens >= 13 * PRODUCTION_INPUT_BUDGET["input_tokens"]
    assert ordinary.max_total_output_tokens >= 13 * 24576


def test_released_production_is_one_catalog_and_old_missing_command_fields_keep_their_meaning():
    production = resolve_behavior()
    assert production.name == DEFAULT == PRODUCTION == "production/1"
    assert production.author_plan_handoff == "approved-story/1"
    assert production.planning_contract is None
    assert production.reader_schema == "listener-experience/5" and production.verification is None
    base = resolve_behavior("narrative-rebuild/1")
    for fmt in ("solo", "dialogue"):
        assert production.instructions("draft", fmt) == base.instructions("draft", fmt)
        assert production.instructions("plan", fmt) == base.instructions("plan", fmt)
        assert production.instructions("plan-revise", fmt) == base.instructions("plan-revise", fmt)
        for role in ("anchor-repair", "plan-target-repair", "fact-review"):
            assert production.instructions(role, fmt) == base.instructions(role, fmt)
    for name in (
        "existing/1",
        "narrative-rebuild/1",
        "narrative-rebuild/2",
        "editorial-candidate/1",
        "editorial-candidate/2",
    ):
        assert resolve_behavior(name).author_plan_handoff is None
        assert input_budget_for_profile(name) == dict(visible_bytes=131072, input_tokens=32768)
    ref = ArtifactRef(kind="ModelProfile", artifact_id=uuid4(), revision_id=uuid4())
    old = WritingRunCommand(
        run_id=uuid4(),
        project_id=uuid4(),
        actor_id=uuid4(),
        case_id="saved",
        author_ref=RuntimeRef(record_kind="user_input", record_id=uuid4()),
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
    )
    saved = command_payload(old)
    assert "plan_mode" not in saved and "behavior_profile" not in saved
    restored = WritingRunCommand.model_validate(saved)
    assert restored.plan_mode == "fixed-plan" and restored.behavior_profile is None
    assert command_payload(restored) == saved
    assert restored.limits.max_total_input_tokens == 196608
    for name in (None, "narrative-rebuild/1", PRODUCTION):
        manifest = dict(role_input_budget=input_budget_for_profile(name))
        if name:
            manifest["behavior_profile"] = name
        assert validate_manifest_budget(manifest) == input_budget_for_profile(name)


class BudgetJournal:
    def __init__(self, body, budget):
        self.body, self.budget = deepcopy(body), budget
        self.events = []

    def request(self, row):
        return self.body

    def manifest(self, run_id):
        return dict(
            input_contract="editorial-role-packets/1",
            role_projection_version="editorial-role-projections/9",
            role_input_budget=self.budget,
        )

    def read(self, ref):
        return dict(
            context=dict(
                role_input_contract="editorial-role-packets/1",
                role_projection_version="editorial-role-projections/9",
            )
        )

    def blob(self, raw):
        return SimpleNamespace(model_dump=lambda **kw: dict(sha256="synthetic"))

    def event(self, row, kind, **data):
        self.events.append(kind)

    def transition(self, row, status, **updates):
        return dict(row, status=status, **updates)


class CountedProvider:
    def __init__(self):
        self.counts, self.submits = 0, 0

    def count(self, body):
        self.counts += 1
        return WireReply(b'{"input_tokens":35737}')

    def submit(self, body, **kwargs):
        self.submits += 1
        return WireReply(
            json.dumps(
                dict(
                    id="resp_production",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(type="message", role="assistant", content=[dict(type="output_text", text="{}")])
                    ],
                    usage=dict(input_tokens=35737, output_tokens=50),
                )
            ).encode()
        )


def test_regular_production_counts_35737_without_manual_retry_and_keeps_context_and_total_reservations():
    row = dict(
        run_id=uuid4(), invocation_id=uuid4(), request_record_id=uuid4(), status="prepared", attempt_index=0
    )
    body = dict(
        model="synthetic",
        instructions="Exact retained instructions.",
        input=[dict(role="user", content=[dict(type="input_text", text="я" * 70000)])],
        text=dict(format=dict(schema=dict(type="object"))),
        metadata={},
    )
    profile = SimpleNamespace(
        output_limit=24576,
        context_limit=200000,
        generation_parameters=dict(store=False),
        timeout_policy=dict(poll_deadline_seconds=10),
    )
    for code, budget, context, limits in [
        (None, PRODUCTION_INPUT_BUDGET, 200000, RunLimits(**PRODUCTION_RUN_LIMITS)),
        (
            "role_visible_byte_budget_exceeded",
            input_budget_for_profile("narrative-rebuild/1"),
            200000,
            RunLimits(),
        ),
        ("context_capacity_exceeded", PRODUCTION_INPUT_BUDGET, 40000, RunLimits(**PRODUCTION_RUN_LIMITS)),
        (
            "run_token_budget_exceeded",
            PRODUCTION_INPUT_BUDGET,
            200000,
            RunLimits(max_total_input_tokens=35736),
        ),
        (
            "run_token_budget_exceeded",
            PRODUCTION_INPUT_BUDGET,
            200000,
            RunLimits(max_total_output_tokens=24575),
        ),
    ]:
        journal, provider = BudgetJournal(body, budget), CountedProvider()
        profile.context_limit = context
        gateway = ModelGateway(journal, lambda _, current=provider: current)
        assert effective_limits(journal, row) == budget
        if code:
            with pytest.raises(RunFailure, match=code):
                gateway.invoke(
                    row, profile, limits=limits, totals=dict(input=0, output=0), guard=lambda: None
                )
            assert provider.submits == 0
        else:
            assert (
                gateway.invoke(
                    row, profile, limits=limits, totals=dict(input=0, output=0), guard=lambda: None
                )
                == "{}"
            )
            assert provider.counts == provider.submits == 1
            assert "count_intent" in journal.events and "submit_intent" in journal.events


def test_ordinary_cli_run_and_historical_resume_build_the_expected_commands(monkeypatch):
    from podgen import writing_cli
    from podgen.runtime.evaluation.run_configuration import production_command

    ref = ArtifactRef(kind="ModelProfile", artifact_id=uuid4(), revision_id=uuid4())
    shared = RuntimeRef(record_kind="user_input", record_id=uuid4())
    project, actor, run_id = uuid4(), uuid4(), uuid4()
    captured = []
    stored = {}

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, command, **kwargs):
            captured.append(command)
            return dict(status="completed_unaccepted")

    monkeypatch.setenv("PODGEN_DATABASE_URL", "unused-by-unit-test")
    monkeypatch.setattr(writing_cli, "Database", lambda *args: None)
    monkeypatch.setattr(writing_cli, "FileObjectStore", lambda *args: None)
    monkeypatch.setattr(writing_cli, "Repository", lambda *args, **kwargs: SimpleNamespace(actor_id=actor))
    monkeypatch.setattr(
        writing_cli,
        "EvaluationJournal",
        lambda *args: SimpleNamespace(run=lambda *args: False, manifest=lambda *args: dict(command=stored)),
    )
    monkeypatch.setattr(writing_cli, "WritingEvaluation", Runner)
    prefix = ["--project", str(project), "--actor", str(actor), "--objects", "/tmp/unused-cli-unit-objects"]
    encoded = ref.model_dump_json()
    assert (
        writing_cli.main(
            [
                *prefix,
                "run",
                "--run-id",
                str(run_id),
                "--case",
                "snow-pump",
                "--author-ref",
                shared.model_dump_json(),
                "--writer-profile",
                encoded,
                "--reviewer-profile",
                encoded,
                "--planner-profile",
                encoded,
                "--plan-reviewer-profile",
                encoded,
                "--export-dir",
                "/tmp/unused-cli-unit-export",
            ]
        )
        == 0
    )
    current = captured[-1]
    assert current.behavior_profile == PRODUCTION and current.plan_mode == "own-plan"
    assert current.review_anchor_policy == "bounded-selection-v1"
    assert current.limits.model_dump(exclude={"schema_version"}) == PRODUCTION_RUN_LIMITS

    historical = WritingRunCommand(
        run_id=run_id,
        project_id=project,
        actor_id=actor,
        case_id="saved",
        author_ref=shared,
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
        input_contract="legacy-author-materials/1",
    )
    stored.update(command_payload(historical))
    assert (
        "behavior_profile" not in stored
        and "plan_mode" not in stored
        and "review_anchor_policy" not in stored
    )
    assert (
        writing_cli.main(
            [*prefix, "resume", "--run-id", str(run_id), "--export-dir", "/tmp/unused-cli-unit-export"]
        )
        == 0
    )
    assert command_payload(captured[-1]) == stored
    assert captured[-1].review_anchor_policy == "strict"
    fresh = WritingRunCommand(
        run_id=run_id,
        project_id=project,
        actor_id=actor,
        case_id="fresh",
        author_ref=shared,
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
    )
    public_python = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=actor,
        case_id="fresh-own-plan",
        author_ref=shared,
        writer_profile_ref=ref,
        reviewer_profile_ref=ref,
        planner_profile_ref=ref,
        plan_reviewer_profile_ref=ref,
    )
    assert public_python.plan_mode == "own-plan"
    assert production_command(public_python).review_anchor_policy == "bounded-selection-v1"
    assert WritingRunCommand.model_validate(command_payload(public_python)).plan_mode == "own-plan"
    admitted = production_command(fresh)
    from pydantic import ValidationError

    misleading = fresh.model_copy(update=dict(review_mode="informed-v1"))
    with pytest.raises(ValidationError, match="editorial_behavior_requires_independent_readings"):
        production_command(misleading)
    assert production_command(misleading, frozen_manifest={}) is misleading
    assert admitted.behavior_profile == PRODUCTION and admitted.review_anchor_policy == "bounded-selection-v1"
    assert command_payload(
        production_command(
            fresh, frozen_manifest=dict(behavior_profile=PRODUCTION, command=command_payload(admitted))
        )
    ) == command_payload(admitted)
    assert production_command(fresh, frozen_manifest={}) == fresh
    assert fresh.review_anchor_policy == "strict"


def test_production_reader_format_is_the_full_verified_late_reader_archive_format():
    from pathlib import Path

    from podgen.runtime.evaluation.preview import preview_request

    from tests.test_v5_role_wire import fixture

    archive = (
        Path(__file__).resolve().parents[2]
        / "evals/reports/2026-11-08-snow-narrative-rebuild/calls/20-reader-review/podgen-request.json"
    )
    saved = json.loads(archive.read_bytes())
    materials, _, _, template, script, *_ = fixture()
    profile = deepcopy(template)
    profile.provider, profile.api_family, profile.adapter_version = (
        "openai",
        "responses",
        "openai-responses-v1",
    )
    profile.model_revision_or_alias = "gpt-6.1-terra"
    profile.context_limit, profile.output_limit = 200000, 24576
    profile.generation_parameters = dict(
        reasoning=dict(effort="medium"), text=dict(verbosity="high"), store=False, background=False
    )
    current = preview_request(uuid4(), "reader-review", profile, materials, draft=script)
    assert resolve_behavior().reader_schema == "listener-experience/5"
    assert current["text"]["format"] == saved["text"]["format"]
