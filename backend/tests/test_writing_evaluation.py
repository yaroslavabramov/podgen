"""Synthetic provider responses exercise real SQL, repository ACLs and the public use case."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.database import one, query
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits, WritingRunCommand
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.writing.materials import AuthorDocument, AuthorMaterials


class FakeProvider:
    def __init__(self, *, fault=None, count=100):
        self.fault, self.tokens = fault, count
        self.requests = []
        self.counts = []
        self.retrieves = []
        self.on_count = None
        self.on_submit = None
        self.last = None
        self.repair_choice = "valid"

    def count(self, body):
        self.counts.append(body)
        if self.on_count:
            self.on_count()
        return WireReply(json.dumps({"input_tokens": self.tokens}).encode())

    def submit(self, body, *, client_request_id):
        self.requests.append(body)
        if self.on_submit:
            self.on_submit()
        if self.fault == "timeout":
            raise ProviderFault("provider_transport_outcome_unknown", ambiguous=True)
        if self.fault == "429":
            raise ProviderFault("provider_http_429", reply=WireReply(b'{"error":{"code":"rate_limit"}}', 429))
        payload = json.loads(body["input"][0]["content"][0]["text"])
        labels = payload["author_materials"]["speaker_labels"]
        stage = payload["stage"]
        output = {
            "utterances": [dict(speaker=s, text=f"СИНТЕТИЧЕСКИЙ полный текст {stage} {s} 🛰️е́") for s in labels]
        }
        if stage == "review":
            output = dict(
                reading_mode="informed",
                listener_answer="СИНТЕТИЧЕСКИЙ ответ",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="СИНТЕТИЧЕСКИЙ тест; длительность не измерена.",
                recommendation="revise",
            )
            if self.fault == "anchor":
                output["strengths"] = [
                    dict(
                        anchors=[dict(utterance_index=999, quote="нет такой цитаты")],
                        reason="SYNTHETIC invalid anchor",
                    )
                ]
        if body["text"]["format"]["name"] == "anchor_choices":
            repair = json.loads(body["input"][-1]["content"][0]["text"])
            selector = repair["selectors"][0]["selector_id"]
            if self.repair_choice == "null":
                selector = None
            elif self.repair_choice == "unknown":
                selector = "invented-selector"
            output = dict(
                choices=[
                    dict(anchor_id=a["anchor_id"], selector_id=selector) for a in repair["invalid_anchors"]
                ]
            )
        response = dict(
            id="resp_" + stage,
            status="completed",
            model=body["model"],
            metadata=body["metadata"],
            output=[
                dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text=json.dumps(output, ensure_ascii=False))],
                )
            ],
            usage=dict(input_tokens=self.tokens, output_tokens=123),
        )
        if self.fault == "incomplete":
            response["status"] = "incomplete"
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        if self.fault == "refusal":
            response["output"][0]["content"] = [dict(type="refusal", refusal="SYNTHETIC refusal")]
        if self.fault == "model":
            response["model"] = "not-the-pinned-model"
        if self.fault == "metadata":
            response["metadata"] = {}
        if self.fault == "nested-output":
            response["output"] = [None]
        if self.fault == "nested-content":
            response["output"][0]["content"] = None
        if self.fault == "invalid-json":
            response["output"][0]["content"][0]["text"] = "{broken json"
        if self.fault == "poll-id":
            response["status"] = "in_progress"
        self.last = response
        return WireReply(json.dumps(response, ensure_ascii=False).encode())

    def retrieve(self, response_id):
        self.retrieves.append(response_id)
        response = self.last | {"id": "resp_another_operation", "status": "completed"}
        return WireReply(json.dumps(response, ensure_ascii=False).encode())


@pytest.fixture
def setup_writer(repo, scope):
    project, _ = scope
    author = RuntimeRef(record_kind="user_input", record_id=uuid4())
    text = "СИНТЕТИЧЕСКОЕ досье\n" + ("Не усекать полный смысл 🛰️е́\n" * 2000) + "ПОСЛЕДНИЙ АБЗАЦ"
    repo.append_runtime(project, author, {"synthetic_author_text": text})

    def loader(generation, project_id, author_ref, *, case_id):
        assert generation.purpose == "generation"
        payload = generation.get_runtime(project_id, author_ref)
        return AuthorMaterials(
            case_id,
            "dialogue",
            ("НИНА", "ИЛЬЯ"),
            (
                AuthorDocument(
                    "author/gps-clock/dossier.md",
                    payload["synthetic_author_text"],
                    "synthetic-sha",
                    author_ref,
                ),
            ),
            output_language="ru",
        )

    profile = create_profile(
        repo, project, model="synthetic-pinned-snapshot", context_limit=1050000, output_limit=65536
    )
    command = WritingRunCommand(
        behavior_profile="existing/1",
        input_contract="legacy-author-materials/1",
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="gps-clock",
        author_ref=author,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
    )
    return command, loader, text


def run(repo, command, loader, provider, *, repair=False):
    return WritingEvaluation(
        repo,
        lambda profile: provider,
        materials_loader=loader,
        legacy_offline_transport=provider,
        gateway_options={"poll_seconds": 0},
    ).run(command, repair_review_anchors=repair)


def test_output_language_comes_from_assignment_and_is_pinned_for_all_roles(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider()

    def english_assignment(*args, **kwargs):
        return replace(loader(*args, **kwargs), output_language="en")

    run(repo, command, english_assignment, provider)
    manifest = EvaluationJournal(repo, command.project_id).manifest(command.run_id)
    assert manifest["output_language"] == "en"
    assert [
        json.loads(req["input"][0]["content"][0]["text"])["output_language"] for req in provider.requests
    ] == ["en"] * 3


def test_normal_full_three_stage_entry_and_idempotency(repo, setup_writer, tmp_path):
    command, loader, text = setup_writer
    provider = FakeProvider()
    result = run(repo, command, loader, provider)
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 3
    assert run(repo, command, loader, provider) == result
    assert len(provider.requests) == 3
    payloads = [json.loads(body["input"][0]["content"][0]["text"]) for body in provider.requests]
    assert all(p["author_materials"] == payloads[0]["author_materials"] for p in payloads)
    assert all(p["author_materials"]["documents"][0]["text"] == text for p in payloads)
    assert [u["index"] for u in payloads[1]["draft"]["utterances"]] == [0, 1]
    assert (
        payloads[2]["editorial_feedback"]
        == EvaluationJournal(repo, command.project_id).output(
            EvaluationJournal(repo, command.project_id).invocation(command.run_id, "review")
        )["output"]
    )
    export = export_run(repo, command.project_id, command.run_id, tmp_path / "export")
    assert export["status"] == "completed_unaccepted"
    assert "СИНТЕТИЧЕСКИЙ полный текст draft" in (tmp_path / "export/draft.txt").read_text()
    assert "СИНТЕТИЧЕСКИЙ полный текст revise" in (tmp_path / "export/revise.txt").read_text()
    assert (tmp_path / "export/review.json").is_file()
    assert (tmp_path / "export/archive/draft/request.json").is_file()
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM acceptance_records WHERE project_id=:p", p=command.project_id)[
                "n"
            ]
            == 0
        )
    changed = command.model_copy(update={"repeat_index": 1})
    with pytest.raises(Conflict, match="run_id_reused"):
        run(repo, changed, loader, provider)
    new_attempt = command.model_copy(update={"run_id": uuid4(), "repeat_index": 1})
    run(repo, new_attempt, loader, provider)
    assert len(provider.requests) == 6


@pytest.mark.parametrize(
    "fault,code",
    [
        ("timeout", "provider_transport_outcome_unknown"),
        ("429", "provider_http_429"),
        ("incomplete", "provider_incomplete"),
        ("refusal", "provider_refused"),
        ("model", "provider_model_revision_mismatch"),
        ("metadata", "provider_response_metadata_mismatch"),
        ("nested-output", "provider_malformed_output"),
        ("nested-content", "provider_malformed_content"),
        ("invalid-json", "role_output_validation_failed"),
        ("poll-id", "provider_response_id_mismatch"),
    ],
)
def test_faults_are_archived_and_never_resubmitted(repo, setup_writer, fault, code):
    command, loader, _ = setup_writer
    if fault == "poll-id":
        profile = create_profile(
            repo,
            command.project_id,
            model="synthetic-pinned-snapshot",
            context_limit=1050000,
            store=True,
            background=True,
        )
        command = command.model_copy(
            update={"writer_profile_ref": profile.ref, "reviewer_profile_ref": profile.ref}
        )
    provider = FakeProvider(fault=fault)
    with pytest.raises(RunFailure, match=code):
        run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    invocation = journal.invocation(command.run_id, "draft")
    assert invocation["output_record_id"] is None
    assert journal.events(invocation)
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider)
    assert len(provider.requests) == 1


def test_wrong_feedback_anchor_stops_before_revision(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure, match="role_output_validation_failed"):
        run(repo, command, loader, provider)
    assert len(provider.requests) == 2
    journal = EvaluationJournal(repo, command.project_id)
    invocation = journal.invocation(command.run_id, "review")
    errors = [
        journal.read(RuntimeRef(record_kind="tool_execution", record_id=e["record_id"]))
        for e in journal.events(invocation)
        if e["event_kind"] == "output_validation_error"
    ]
    assert errors[0]["data"]["details"] == [{"code": "feedback_utterance_index_out_of_range"}]


def test_capacity_and_budget_fail_without_truncation_or_submit(repo, setup_writer):
    command, loader, text = setup_writer
    provider = FakeProvider(count=1050000)
    with pytest.raises(RunFailure, match="context_capacity_exceeded"):
        run(repo, command, loader, provider)
    assert not provider.requests
    journal = EvaluationJournal(repo, command.project_id)
    request = journal.request(journal.invocation(command.run_id, "draft"))
    assert (
        text
        in json.loads(request["input"][0]["content"][0]["text"])["author_materials"]["documents"][0]["text"]
    )
    command = command.model_copy(update={"run_id": uuid4(), "limits": RunLimits(max_total_input_tokens=10)})
    provider = FakeProvider(count=100)
    with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
        run(repo, command, loader, provider)
    assert not provider.requests


def test_live_membership_check_after_token_count(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider()

    def revoke():
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=command.project_id,
                a=repo.actor_id,
            )

    provider.on_count = revoke
    with pytest.raises(AccessDenied):
        run(repo, command, loader, provider)
    assert len(provider.counts) == 1 and not provider.requests


def test_same_invocation_after_submit_crash_never_sends_twice(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider()

    def crash():
        raise KeyboardInterrupt("SYNTHETIC crash after persisted submit intent")

    provider.on_submit = crash
    with pytest.raises(KeyboardInterrupt):
        run(repo, command, loader, provider)
    provider.on_submit = None
    with pytest.raises(RunFailure, match="submission_acknowledgement_unknown"):
        run(repo, command, loader, provider)
    assert len(provider.requests) == 1


def test_acl_revocation_after_draft_prevents_review_send(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider()
    original = provider.submit

    def submit(body, *, client_request_id):
        reply = original(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == "draft":
            with repo.database.transaction() as tx:
                query(
                    tx,
                    "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                    p=command.project_id,
                    a=repo.actor_id,
                )
        return reply

    provider.submit = submit
    with pytest.raises(AccessDenied):
        run(repo, command, loader, provider)
    assert len(provider.requests) == 1


def test_source_revoked_during_count_is_not_resubmitted_from_evaluator_request(
    repo, scope, producer, setup_writer
):
    from podgen.persistence.dependencies import DependencyLedger

    from tests.factories import Factory

    command, loader, text = setup_writer
    f = Factory(repo, *scope, producer).setup()
    source, _ = f.source()
    bound = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(
        command.project_id,
        bound,
        dict(synthetic_author_text=text, basis_ref=source.ref.model_dump(mode="json")),
    )
    command = command.model_copy(update={"author_ref": bound})
    provider = FakeProvider()
    provider.on_count = lambda: DependencyLedger(repo).invalidate(
        command.project_id,
        source.ref,
        category="access",
        reason="SYNTHETIC source withdrawal during token count",
    )
    with pytest.raises(AccessDenied):
        run(repo, command, loader, provider)
    assert not provider.requests and len(provider.counts) == 1
    provider.on_count = None
    with pytest.raises(AccessDenied):
        run(repo, command, loader, provider)
    assert not provider.requests and len(provider.counts) == 1


def test_resume_uses_archived_prompts_after_editor_publishes_new_version(
    repo, setup_writer, monkeypatch, tmp_path
):

    command, loader, _ = setup_writer
    provider = FakeProvider()
    original = provider.submit

    def stop_after_first_response(body, *, client_request_id):
        reply = original(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == "review":
            raise KeyboardInterrupt("SYNTHETIC interrupted submitted review")
        return reply

    provider.submit = stop_after_first_response
    with pytest.raises(KeyboardInterrupt):
        run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    manifest = journal.manifest(command.run_id)
    old_instructions = journal.request(journal.invocation(command.run_id, "draft"))["instructions"]
    saved_revise_ref = manifest["prompt_refs"]["revise"]
    from podgen.domain.contracts.common import ArtifactRef

    saved_revise_body = repo.get(command.project_id, ArtifactRef.model_validate(saved_revise_ref)).payload[
        "template"
    ]
    assert journal.invocation(command.run_id, "revise") is None
    assert "role_instructions_version" not in manifest
    import shutil

    from podgen.writing import prompts

    resources = tmp_path / "new-resources"
    shutil.copytree(prompts.files("podgen.writing.prompts"), resources)
    revise_file = resources / "current/revise.md"
    revise_file.write_bytes(revise_file.read_bytes() + b"\nSYNTHETIC newly edited revision instructions.\n")
    monkeypatch.setattr(prompts, "files", lambda _: resources)
    assert prompts.role_instructions("revise", "dialogue") != saved_revise_body
    monkeypatch.setattr(
        __import__("podgen.runtime.evaluation.manifest", fromlist=["resolve_behavior"]),
        "resolve_behavior",
        lambda *args: pytest.fail("must not read current resources for an existing run"),
    )
    # A complete stored run must not inspect the newly published role templates.
    # Reconcile the interrupted review only from its original archived provider response.
    invocation = journal.invocation(command.run_id, "review")
    body = journal.request(invocation)
    provider.submit = original
    reply = original(
        body, client_request_id=str(invocation["invocation_id"])
    )  # synthetic reconciliation fixture, no live API
    from podgen.runtime.models.gateway import ModelGateway

    ModelGateway(journal, lambda profile: provider)._archive_reply(invocation, reply, body)
    calls = len(provider.requests)
    run(repo, command, loader, provider)
    assert len(provider.requests) == calls + 1  # only the previously unsent revision
    assert journal.request(journal.invocation(command.run_id, "draft"))["instructions"] == old_instructions
    revision = journal.request(journal.invocation(command.run_id, "revise"))
    assert revision["instructions"] == saved_revise_body
    assert journal.manifest(command.run_id)["prompt_refs"]["revise"] == saved_revise_ref
    assert (
        journal.manifest(command.run_id)["role_instructions_versions"]
        == manifest["role_instructions_versions"]
    )


def test_late_completed_replay_ignores_new_budgets_compiler_and_prompts(
    repo, setup_writer, monkeypatch, tmp_path
):
    from datetime import datetime, timedelta, timezone

    from podgen.runtime.evaluation import service

    command, loader, _ = setup_writer
    provider = FakeProvider()
    result = run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    before = journal.run(command.run_id)

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(timezone.utc) + timedelta(days=1)

    monkeypatch.setattr(service, "datetime", FutureDatetime)
    monkeypatch.setattr(service, "COMPILER_VERSION", "some-future-compiler")
    monkeypatch.setattr(
        __import__("podgen.runtime.evaluation.manifest", fromlist=["resolve_behavior"]),
        "resolve_behavior",
        lambda *args: pytest.fail("must not read live prompts"),
    )

    def unavailable_loader(*args, **kwargs):
        pytest.fail("must not recompile materials for an already completed attempt")

    app = WritingEvaluation(
        repo,
        lambda profile: pytest.fail("must not instantiate provider"),
        materials_loader=unavailable_loader,
    )
    assert app.run(command) == result
    assert journal.run(command.run_id) == before
    assert len(provider.requests) == 3
    export_run(repo, command.project_id, command.run_id, tmp_path / "ours")
    marker = tmp_path / "ours/.podgen-writing-export.json"
    marker.write_text(
        json.dumps(
            {
                "contract": "writing-evaluation-export-v1",
                "project_id": str(command.project_id),
                "run_id": str(uuid4()),
            }
        )
    )
    with pytest.raises(Conflict, match="export_directory_belongs_to_another_run"):
        export_run(repo, command.project_id, command.run_id, tmp_path / "ours")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "draft.txt").write_text("USER AUTHORED FILE")
    with pytest.raises(Conflict, match="export_directory_not_empty"):
        export_run(repo, command.project_id, command.run_id, foreign)
    assert (foreign / "draft.txt").read_text() == "USER AUTHORED FILE"
