"""A remote blob read must not publish another batch member using stale authorization."""

from io import BytesIO
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.runtime.evaluation import export_archive
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.models.contracts import WireReply
from tests.authorization_helpers import withdraw_blob
from tests.test_runtime_operation_boundaries import RemoteStore


def test_remote_A_revoke_B_without_B_blob_field_discards_event_export(repo, scope, tmp_path, monkeypatch):
    project, _ = scope
    source = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, source, dict(frozen="independent source"))
    private_blob = repo.save_blob(project, BytesIO(b"Synthetic private B-only dependency"), "text/plain")
    private = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(project, private, dict(blob=private_blob.model_dump(mode="json")))
    profile = create_profile(repo, project, model="gpt-6.1-sol", context_limit=100000).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="remote-export-boundary",
        author_ref=source,
        writer_profile_ref=profile,
        reviewer_profile_ref=profile,
    )
    journal = EvaluationJournal(repo, project)
    journal.create_run(command, dict(command=command_payload(command)))
    invocation = journal.prepare(command.run_id, "fact-review", {}, {})
    A = journal.event(invocation, "probe_A", reply=WireReply(b'{"safe":"A response"}'))
    B = journal.event(invocation, "probe_B", data=dict(private_dependency=private.model_dump(mode="json")))
    assert "response_blob" not in journal.read(B)
    events = [
        dict(record_id=A.record_id, event_kind="probe_A"),
        dict(record_id=B.record_id, event_kind="probe_B"),
    ]
    monkeypatch.setattr(export_archive, "event_pages", lambda *args: iter([events]))
    repo.objects = RemoteStore(repo, lambda: withdraw_blob(repo, project, private_blob))
    with pytest.raises(AccessDenied):
        export_archive._events(journal, invocation, tmp_path / "archive", tmp_path)
    assert not (tmp_path / "archive/events.json").exists()
    assert repo._read_authorization.get() is None
