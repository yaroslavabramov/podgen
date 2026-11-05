"""Pin repair-only instructions before any generation; preserve historical fallback and attempts."""

import hashlib
import shutil
from dataclasses import asdict
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.review_repair import LEGACY_REPAIR_INSTRUCTIONS_V1, prepare_anchor_repair
from podgen.writing import prompts
from tests.test_writing_evaluation import FakeProvider, run
from tests.test_writing_evaluation import setup_writer as setup_writer


def test_legacy_repair_suffix_is_byte_identical():
    assert hashlib.sha256(LEGACY_REPAIR_INSTRUCTIONS_V1.encode()).hexdigest() == (
        "b0ce4671ccc75b88e77889bc92223a5feb4b841a329374bf696e654f478161fa"
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_edit_repair_file_pins_new_runs_and_never_rebuilds_existing_attempt(
    repo, setup_writer, tmp_path, monkeypatch, legacy
):
    resources = tmp_path / "resources"
    shutil.copytree(prompts.files("podgen.writing.prompts"), resources)
    monkeypatch.setattr(prompts, "files", lambda _: resources)
    initial_suffix = prompts.load_anchor_repair_instructions()
    initial_roles = prompts.load_instruction_set("dialogue")
    create_run = EvaluationJournal.create_run
    if legacy:

        def legacy_manifest(journal, command, manifest):
            old = dict(manifest)
            old.pop("anchor_repair_instructions")
            return create_run(journal, command, old)

        monkeypatch.setattr(EvaluationJournal, "create_run", legacy_manifest)
    command, loader, _ = setup_writer
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure, match="role_output_validation_failed"):
        run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    manifest = journal.manifest(command.run_id)
    if legacy:
        assert "anchor_repair_instructions" not in manifest
    else:
        assert manifest["anchor_repair_instructions"] == asdict(initial_suffix)
    monkeypatch.setattr(EvaluationJournal, "create_run", create_run)

    active_file = resources / "current/anchor-repair.md"
    active_file.write_bytes(active_file.read_bytes() + b"\nSYNTHETIC newly edited selector guidance.\r\n")
    edited_suffix = prompts.load_anchor_repair_instructions()
    assert edited_suffix.version != initial_suffix.version
    assert prompts.load_instruction_set("dialogue") == initial_roles
    later = command.model_copy(update={"run_id": uuid4()})
    with pytest.raises(RunFailure, match="role_output_validation_failed"):
        run(repo, later, loader, FakeProvider(fault="anchor"))
    later_manifest = journal.manifest(later.run_id)
    assert later_manifest["anchor_repair_instructions"] == asdict(edited_suffix)
    assert later_manifest["role_instructions_versions"] == manifest["role_instructions_versions"]

    draft_invocation = journal.invocation(command.run_id, "draft")
    original_review = journal.invocation(command.run_id, "review", 0)
    original_body = journal.request(original_review)["instructions"]
    draft_ref = RuntimeRef(record_kind="tool_execution", record_id=draft_invocation["output_record_id"])
    invocation = prepare_anchor_repair(
        journal,
        original_review,
        draft=journal.output(draft_invocation)["output"],
        draft_ref=draft_ref.model_dump(mode="json"),
        guard=lambda: None,
    )
    saved_request = journal.request(invocation)
    expected_suffix = LEGACY_REPAIR_INSTRUCTIONS_V1 if legacy else initial_suffix.body
    assert saved_request["instructions"] == original_body + expected_suffix
    context = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    )["context"]
    assert context["anchor_repair_instructions_version"] == (
        "anchor-repair/1" if legacy else initial_suffix.version
    )
    # Further resource changes and even complete loss cannot rebuild this prepared attempt.
    shutil.rmtree(resources)
    monkeypatch.setattr(prompts, "files", lambda _: pytest.fail("resume must not read current instructions"))
    result = run(repo, command, loader, provider, repair=True)
    assert result["status"] == "completed_unaccepted"
    assert len(provider.requests) == 4
    assert provider.requests[2] == saved_request
    assert journal.request(invocation) == saved_request
    assert journal.manifest(command.run_id) == manifest
    assert run(repo, command, loader, provider, repair=True) == result
    assert len(provider.requests) == 4
