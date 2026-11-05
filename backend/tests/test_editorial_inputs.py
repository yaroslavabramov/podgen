"""Offline admission, role isolation and budget checks against real persistence."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.editorial_documents import import_document
from podgen.persistence.editorial_inputs import (
    generation_repo,
    import_fixed,
    import_shared,
    load_editorial_materials,
)
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from podgen.writing.projections import role_payload
from tests.test_writing_evaluation import setup_writer as setup_writer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def editorial(repo, scope):
    project, _ = scope
    data = json.loads((ROOT / "backend/schemas/editorial-input.v1.example.json").read_text())
    raw = "Точная цитата.".encode()
    ref = import_document(
        repo,
        project,
        raw,
        source_path="sources/example/source.txt",
        purpose="primary",
        scope_key="new-independent-assignment",
    )
    data["documents"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    data["dossier"]["knowledge_cards"][0]["excerpts"][0]["end"] = len(raw.decode())
    mapping = {data["documents"][0]["source_path"]: ref.model_dump(mode="json")}
    shared = import_shared(
        repo, project, data, mapping, original_bytes=json.dumps(data, ensure_ascii=False, indent=3).encode()
    )
    fixed = json.loads((ROOT / "evals/editorial-inputs/v1/gps-clock/fixed-plan.json").read_text())
    fixed["assignment_key"] = data["assignment"]["key"]
    fixed["shared_input_digest"] = digest(data)
    fixed["document"] = {
        "source_path": "editorial/editor-plan.txt",
        "sha256": hashlib.sha256(b"Exact fixed source").hexdigest(),
    }
    plan = fixed["plan"]
    plan["plan"]["host_functions"] = plan["plan"]["host_functions"][:1]
    plan["plan"]["host_functions"][0]["speaker_id"] = "ВЕДУЩИЙ"
    plan["plan"]["opening"]["material_refs"] = ["K1"]
    for act in plan["plan"]["acts"]:
        act["material_refs"] = ["K1"]
    planref = import_document(
        repo,
        project,
        b"Exact fixed source",
        source_path=fixed["document"]["source_path"],
        purpose="fixed_plan",
        scope_key="new-independent-assignment",
    )
    mapping[fixed["document"]["source_path"]] = planref.model_dump(mode="json")
    fixed_ref = import_fixed(repo, project, fixed, mapping, shared)
    return project, data, mapping, shared, fixed_ref, plan


def test_role_views_preserve_meaning_and_keep_backend_private(repo, editorial):
    project, data, mapping, shared, fixed, plan = editorial
    materials = load_editorial_materials(
        generation_repo(repo), project, shared, case_id=data["assignment"]["key"], fixed_ref=fixed
    )
    writer = role_payload("draft", materials)
    wire = json.dumps(writer, ensure_ascii=False)
    assert "Exact fixed source" not in wire
    assert "source_path" not in wire and "sha256" not in wire and str(shared.record_id) not in wire
    assert "Точная цитата." not in wire
    assert writer["support"][0]["claim"] == data["dossier"]["knowledge_cards"][0]["claim"]
    assert writer["assignment"]["duration"] == dict(target_seconds=900, min_seconds=780, max_seconds=1080)
    assert "working_answer" not in wire and "route_summary" not in wire and "main_question" not in wire
    draft = {"utterances": [{"speaker": "ВЕДУЩИЙ", "text": "Весь сценарий без усечения."}]}
    feedback = dict(
        recommendation="keep",
        listener_answer="Ответ",
        important_limits=[],
        findings=[],
        research_requests=[],
        duration_note="Не измерена",
        strengths=[],
    )
    review = role_payload("review", materials, draft=draft)
    revise = role_payload("revise", materials, draft=draft, feedback=feedback)
    assert len(review["evidence"]) == 1 and review["evidence"][0]["text"] == "Точная цитата."
    assert revise["revision_guidance"]["recommendation"] == "keep"
    assert revise["draft"]["utterances"][0]["text"] == draft["utterances"][0]["text"]
    assert "candidate_plan" not in review and "story_intent" in review


def test_exact_original_repeat_and_concurrent_import(repo, editorial):
    project, data, mapping, shared, _, _ = editorial
    raw = json.dumps(data, ensure_ascii=False, indent=3).encode()

    def admit(_):
        return import_shared(repo, project, data, mapping, original_bytes=raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(admit, range(2))) == [shared, shared]
    first = deepcopy(data)
    first["assignment"]["key"] = "first-concurrent-admission"
    first_raw = json.dumps(first, ensure_ascii=False, indent=2).encode()

    def new_admission(_):
        return import_shared(repo, project, first, mapping, original_bytes=first_raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = list(pool.map(new_admission, range(2)))
    assert imported[0] == imported[1] and imported[0] != shared
    saved = repo.get_runtime(project, shared)
    from podgen.domain.contracts.common import BlobRef

    assert (
        repo.read_runtime_blob(project, shared, BlobRef.model_validate(saved["original_input_blob"])) == raw
    )


def test_out_of_range_excerpt_and_fixed_alias_rejected(repo, editorial):
    project, data, mapping, shared, fixed, _ = editorial
    invalid = deepcopy(data)
    invalid["dossier"]["knowledge_cards"][0]["excerpts"][0]["end"] += 100
    with pytest.raises(ContractError, match="editorial_excerpt_mismatch"):
        import_shared(repo, project, invalid, mapping)
    invalid = deepcopy(data)
    original = b"Exact fixed source"
    alias = import_document(
        repo,
        project,
        original,
        source_path="different-source.txt",
        purpose="primary",
        scope_key="new-independent-assignment",
    )
    invalid["documents"][0].update(
        source_path="different-source.txt", sha256=hashlib.sha256(original).hexdigest()
    )
    with pytest.raises(AccessDenied):
        import_shared(repo, project, invalid, {"different-source.txt": alias.model_dump(mode="json")})


class LocalProvider:
    def __init__(self, tokens=100):
        self.requests = []
        self.counts = []
        self.tokens = tokens

    def count(self, body):
        self.counts.append(body)
        return WireReply(json.dumps({"input_tokens": self.tokens}).encode())

    def submit(self, body, *, client_request_id):
        self.requests.append(body)
        stage = body["metadata"]["podgen_stage"]
        output = {"utterances": [{"speaker": "ВЕДУЩИЙ", "text": "Сохраняем полный текст."}]}
        if stage == "review":
            output = dict(
                reading_mode="informed",
                listener_answer="Ответ",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="Не измерена",
                recommendation="keep",
            )
        return WireReply(
            json.dumps(
                dict(
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
                    usage=dict(input_tokens=self.tokens, output_tokens=20),
                ),
                ensure_ascii=False,
            ).encode()
        )


def test_default_real_runtime_and_resume_are_projected(repo, editorial):
    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=fixed,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
    )
    provider = LocalProvider()
    service = WritingEvaluation(repo, lambda _: provider)
    assert service.run(command)["status"] == "completed_unaccepted"
    service.run(command)
    assert len(provider.requests) == 2
    assert all("author_materials" not in r["input"][0]["content"][0]["text"] for r in provider.requests)
    assert (
        EvaluationJournal(repo, project).manifest(command.run_id)["input_contract"]
        == "editorial-role-packets/1"
    )


def test_token_guard_counts_but_does_not_submit(repo, editorial):
    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=fixed,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
    )
    provider = LocalProvider(tokens=32769)
    from podgen.runtime.evaluation.contracts import RunFailure

    with pytest.raises(RunFailure, match="role_input_token_budget_exceeded"):
        WritingEvaluation(repo, lambda _: provider).run(command)
    assert len(provider.counts) == 1 and not provider.requests


def test_byte_guard_before_provider_construction(repo, editorial):
    project, data, mapping, _, _, plan = editorial
    data = deepcopy(data)
    data["assignment"]["style_guidance"] = ["Осмысленный полный текст без усечения. " * 6000]
    shared = import_shared(repo, project, data, mapping)
    profile = create_profile(
        repo,
        project,
        model="local-double",
        context_limit=1050000,
        output_limit=32768,
        roles=["planner", "plan_reviewer"],
    )
    writer = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        writer_profile_ref=writer.ref,
        reviewer_profile_ref=writer.ref,
        plan_mode="own-plan",
        planner_profile_ref=profile.ref,
        plan_reviewer_profile_ref=profile.ref,
    )
    from podgen.runtime.evaluation.contracts import RunFailure

    constructed = []
    with pytest.raises(RunFailure, match="role_visible_byte_budget_exceeded"):
        WritingEvaluation(repo, lambda _: constructed.append(True)).run(command)
    assert not constructed
    journal = EvaluationJournal(repo, project)
    invocation = journal.invocation(command.run_id, "plan")
    assert invocation["status"] == "validation_failed"
    payload = json.loads(journal.request(invocation)["input"][0]["content"][0]["text"])
    assert payload["assignment"]["style_guidance"] == data["assignment"]["style_guidance"]


def test_own_plan_uses_only_shared_packet_and_structured_creative_decisions(repo, editorial):
    project, data, _, shared, fixed, _ = editorial
    from tests.own_plan_provider import proposal

    class Planner(LocalProvider):
        def submit(self, body, *, client_request_id):
            stage = body["metadata"]["podgen_stage"]
            if not stage.startswith("plan"):
                return super().submit(body, client_request_id=client_request_id)
            self.requests.append(body)
            payload = json.loads(body["input"][0]["content"][0]["text"])
            if stage == "plan":
                output = proposal(
                    dict(material_catalog=payload["material_catalog"], speaker_labels=["ВЕДУЩИЙ"]), stage
                )
            else:
                output = dict(
                    decision="accept",
                    decision_reason="EDITOR PRAISE NEVER FOR WRITER",
                    listener_answer="Ответ",
                    important_limits=[],
                    strengths=[],
                    findings=[],
                    research_requests=[],
                    duration_note="Не измерена",
                )
            return WireReply(
                json.dumps(
                    dict(
                        id="resp_" + stage,
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=[
                            dict(
                                type="message",
                                role="assistant",
                                content=[
                                    dict(type="output_text", text=json.dumps(output, ensure_ascii=False))
                                ],
                            )
                        ],
                        usage=dict(input_tokens=self.tokens, output_tokens=20),
                    ),
                    ensure_ascii=False,
                ).encode()
            )

    writer = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    planner = create_profile(
        repo,
        project,
        model="local-double",
        context_limit=1050000,
        output_limit=32768,
        roles=["planner", "plan_reviewer"],
    )
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        writer_profile_ref=writer.ref,
        reviewer_profile_ref=writer.ref,
        plan_mode="own-plan",
        planner_profile_ref=planner.ref,
        plan_reviewer_profile_ref=planner.ref,
    )
    original = repo.get_runtime(project, fixed)
    provider = Planner()
    service = WritingEvaluation(repo, lambda _: provider)
    result = service.run(command)
    assert result["status"] == "completed_unaccepted"
    service.run(command)
    assert len(provider.requests) == 4
    for request in provider.requests:
        wire = json.dumps(request, ensure_ascii=False)
        assert "Exact fixed source" not in wire and str(fixed.record_id) not in wire
        if request["metadata"]["podgen_stage"] in ("draft", "review", "revise"):
            assert "EDITOR PRAISE NEVER FOR WRITER" not in wire
    assert repo.get_runtime(project, fixed) == original


@pytest.mark.parametrize("malicious", [False, True])
def test_compact_anchor_repair_rejects_unoffered_real_selector(repo, editorial, malicious):
    project, data, _, shared, fixed, _ = editorial
    script = dict(
        utterances=[
            dict(speaker="ВЕДУЩИЙ", text="Первое предложение. Второе предложение."),
            dict(speaker="ВЕДУЩИЙ", text="Другая реплика без замечания."),
        ]
    )

    class RepairProvider(LocalProvider):
        def submit(self, body, *, client_request_id):
            kind = body["text"]["format"]["name"]
            reply = (
                super().submit(body, client_request_id=client_request_id)
                if kind != "anchor_choices"
                else None
            )
            stage = body["metadata"]["podgen_stage"]
            if kind == "anchor_choices":
                self.requests.append(body)
                payload = json.loads(body["input"][0]["content"][0]["text"])
                assert len(body["input"]) == 1 and "rejected_feedback" not in payload
                assert all(row["selector_id"].startswith("u0:") for row in payload["selectors"])
                assert len(payload["selectors"]) > 1
                selected = (
                    f"u1:0-{len(script['utterances'][1]['text'])}"
                    if malicious
                    else payload["selectors"][1]["selector_id"]
                )
                output = dict(
                    choices=[dict(anchor_id=payload["invalid_anchors"][0]["anchor_id"], selector_id=selected)]
                )
                response = dict(
                    id="resp_repair",
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
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            else:
                response = json.loads(reply.body)
                output = json.loads(response["output"][0]["content"][0]["text"])
                if stage == "draft":
                    output = script
                if stage == "review":
                    output["recommendation"] = "revise"
                    output["findings"] = [
                        dict(
                            dimension="oral_style",
                            priority="important",
                            anchors=[dict(utterance_index=0, quote="Ошибочная цитата")],
                            observation="Неясный переход.",
                            requested_change="Прояснить переход.",
                            preserve=[],
                        )
                    ]
                response["output"][0]["content"][0]["text"] = json.dumps(output, ensure_ascii=False)
            return WireReply(json.dumps(response, ensure_ascii=False).encode())

    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=fixed,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
        review_anchor_policy="bounded-selection-v1",
    )
    provider = RepairProvider()
    from podgen.runtime.evaluation.contracts import RunFailure

    if malicious:
        with pytest.raises(RunFailure, match="unoffered_anchor_selector"):
            WritingEvaluation(repo, lambda _: provider).run(command)
        assert len(provider.requests) == 3
    else:
        assert WritingEvaluation(repo, lambda _: provider).run(command)["status"] == "completed_unaccepted"
        assert len(provider.requests) == 4


def test_keep_finalization_is_free_exact_and_exported(repo, editorial, tmp_path):
    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id=data["assignment"]["key"],
        author_ref=shared,
        fixed_plan_input_ref=fixed,
        writer_profile_ref=profile.ref,
        reviewer_profile_ref=profile.ref,
    )
    provider = LocalProvider()
    service = WritingEvaluation(repo, lambda _: provider)
    result = service.run(command)
    assert result == service.run(command)
    journal = EvaluationJournal(repo, project)
    assert journal.invocation(command.run_id, "revise") is None
    assert result["finalization"]["model_call"] is False
    from podgen.runtime.evaluation.export import export_run

    exported = export_run(repo, project, command.run_id, tmp_path / "export")
    assert set(exported["invocations"]) == {"draft", "review"}
    assert (tmp_path / "export/final.txt").read_bytes() == (tmp_path / "export/draft.txt").read_bytes()
    assert not (tmp_path / "export/archive/revise").exists()
    final = json.loads((tmp_path / "export/final.json").read_text())
    assert final["origin"] == "unchanged_draft" and "invocation_id" not in final


@pytest.mark.parametrize(
    "recommendation,research,findings,error",
    [
        ("keep", [], [dict(priority="important")], "contradictory_editorial_decision"),
        ("keep", [dict(question="Нужна проверка")], [], "contradictory_editorial_decision"),
        ("needs_material", [dict(question="Нужна проверка")], [], "editorial_needs_material"),
        ("revise", [], [], "contradictory_editorial_decision"),
    ],
)
def test_ambiguous_or_missing_material_review_never_generates(recommendation, research, findings, error):
    from podgen.runtime.evaluation.contracts import RunFailure
    from podgen.runtime.evaluation.finalization import review_action

    with pytest.raises(RunFailure, match=error):
        review_action(dict(recommendation=recommendation, research_requests=research, findings=findings))


def test_new_raw_run_is_disabled_before_transport(repo, setup_writer):
    command, loader, _ = setup_writer
    constructed = []
    with pytest.raises(ContractError, match="legacy_outbound_disabled_start_compact_run"):
        WritingEvaluation(repo, lambda _: constructed.append(True), materials_loader=loader).run(command)
    assert not constructed


def test_forged_shared_envelope_cannot_open_fixed_source(repo, editorial, monkeypatch):
    from uuid import uuid5

    from podgen.domain.contracts.common import RuntimeRef

    project, data, _, shared, fixed, _ = editorial
    original = repo.get_runtime(project, shared)
    fixed_source = repo.get_runtime(project, fixed)["source_ref"]
    forged = deepcopy(original)
    forged["source_refs"]["primary"] = fixed_source
    ref = RuntimeRef(record_kind="user_input", record_id=uuid5(project, "editorial-input:" + digest(forged)))
    repo.append_runtime(project, ref, forged)
    generation = generation_repo(repo)
    original_get = generation.get_runtime

    def checked(project_id, selected):
        assert selected.record_id != RuntimeRef.model_validate(fixed_source).record_id, "excluded body opened"
        return original_get(project_id, selected)

    monkeypatch.setattr(generation, "get_runtime", checked)
    with pytest.raises(AccessDenied):
        load_editorial_materials(generation, project, ref, case_id=data["assignment"]["key"])


def test_pending_raw_resume_is_disabled_but_archive_preserved(repo, setup_writer):
    from podgen.runtime.evaluation.contracts import RunFailure
    from tests.test_writing_evaluation import FakeProvider

    command, loader, _ = setup_writer
    provider = FakeProvider(fault="429")
    with pytest.raises(RunFailure):
        WritingEvaluation(
            repo, lambda _: None, materials_loader=loader, legacy_offline_transport=provider
        ).run(command)
    journal = EvaluationJournal(repo, command.project_id)
    original = journal.request(journal.invocation(command.run_id, "draft"))
    constructed = []
    with pytest.raises(ContractError, match="legacy_outbound_disabled_start_compact_run"):
        WritingEvaluation(repo, lambda _: constructed.append(True), materials_loader=loader).run(command)
    assert not constructed
    assert journal.request(journal.invocation(command.run_id, "draft")) == original


@pytest.mark.parametrize("case", ["gps-clock", "snow-pump"])
def test_real_editorial_packets_bind_every_exact_source_and_fit(repo, scope, case):
    from podgen.persistence.editorial_staging import EditorialStaging
    from podgen.runtime.evaluation.preview import preview_request
    from podgen.runtime.evaluation.profiles import load_profile
    from podgen.runtime.evaluation.wire_budget import check_visible, visible_sizes
    from tests.test_writing_cli import ROOT as CORPUS

    project, _ = scope
    report = EditorialStaging(repo).import_directory(project, CORPUS)
    shared_bytes = (ROOT / f"evals/editorial-inputs/v1/{case}/shared.json").read_bytes()
    data = json.loads(shared_bytes)
    shared = import_shared(repo, project, data, report["file_refs"], original_bytes=shared_bytes)
    fixed_bytes = (ROOT / f"evals/editorial-inputs/v1/{case}/fixed-plan.json").read_bytes()
    fixed = import_fixed(
        repo, project, json.loads(fixed_bytes), report["file_refs"], shared, original_bytes=fixed_bytes
    )
    generation = generation_repo(repo)
    own = load_editorial_materials(generation, project, shared, case_id=case)
    assert all(not doc.path.endswith("/plan.md") for doc in own.documents)
    materials = load_editorial_materials(generation, project, shared, case_id=case, fixed_ref=fixed)
    profile = create_profile(
        repo,
        project,
        model="local-double",
        context_limit=1050000,
        output_limit=32768,
        roles=["writer", "editorial_reviewer", "planner", "plan_reviewer"],
    )
    parsed_profile = load_profile(generation, project, profile.ref, "draft")
    draft = dict(
        utterances=[dict(speaker=materials.speaker_labels[0], text="Полный текст для структурной проверки.")]
    )
    feedback = dict(
        recommendation="keep",
        listener_answer="Ответ",
        important_limits=[],
        strengths=[],
        findings=[],
        research_requests=[],
        duration_note="Не измерена",
    )
    for stage in ("draft", "review", "revise"):
        body = preview_request(uuid4(), stage, parsed_profile, materials, draft=draft, feedback=feedback)
        check_visible(body, "editorial-role-packets/1")
        assert visible_sizes(body)["total"] < 128 * 1024
    plan = preview_request(uuid4(), "plan", parsed_profile, own)
    wire = json.dumps(plan, ensure_ascii=False)
    assert "/plan.md" not in wire and "fixed_input" not in wire


def test_default_cli_preview_and_keep_run_need_no_real_provider(repo, editorial, tmp_path, monkeypatch):
    from podgen import writing_cli

    project, data, _, shared, fixed, _ = editorial
    profile = create_profile(repo, project, model="local-double", context_limit=1050000, output_limit=32768)
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LocalProvider()
    constructed = []

    def local_provider(*args, **kwargs):
        constructed.append(True)
        return provider

    monkeypatch.setattr(writing_cli, "OpenAIProvider", local_provider)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    shared_json = json.dumps(shared.model_dump(mode="json"))
    fixed_json = json.dumps(fixed.model_dump(mode="json"))
    profile_json = json.dumps(profile.ref.model_dump(mode="json"))
    preview = tmp_path / "preview.json"
    assert (
        writing_cli.main(
            prefix
            + [
                "preview",
                "--shared-ref",
                shared_json,
                "--fixed-ref",
                fixed_json,
                "--case",
                data["assignment"]["key"],
                "--stage",
                "draft",
                "--profile",
                profile_json,
                "--run-id",
                str(uuid4()),
                "--out",
                str(preview),
            ]
        )
        == 0
    )
    assert not constructed and not provider.counts
    wire = json.loads(preview.read_text())
    assert wire["endpoint_called"] is False and wire["visible_bytes"]["total"] < 128 * 1024
    run_id = uuid4()
    export = tmp_path / "cli-export"
    assert (
        writing_cli.main(
            prefix
            + [
                "run",
                "--run-id",
                str(run_id),
                "--case",
                data["assignment"]["key"],
                "--author-ref",
                shared_json,
                "--fixed-plan-input-ref",
                fixed_json,
                "--writer-profile",
                profile_json,
                "--reviewer-profile",
                profile_json,
                "--export-dir",
                str(export),
            ]
        )
        == 0
    )
    assert len(provider.requests) == 2 and len(constructed) == 2
    final = json.loads((export / "final.json").read_text())
    assert final["model_call"] is False
    journal = EvaluationJournal(repo, project)
    assert journal.manifest(run_id)["command"]["input_contract"] == "editorial-role-packets/1"
    assert journal.manifest(run_id)["command"]["limits"]["max_total_input_tokens"] == 196608
    assert any(
        event["event_kind"] == "role_input_measurement"
        for event in journal.events(journal.invocation(run_id, "draft"))
    )
