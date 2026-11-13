"""Cold metadata restore bounds full bodies and never replaces fresh authorization."""

from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.errors import AccessDenied
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.models.research_history import ResearchHistory
from tests.authorization_helpers import withdraw_blob
from tests.test_read_authorization import diamond


def test_cold_restore_reuses_provenance_discards_bodies_and_rechecks_withdrawal(repo, scope):
    project, _ = scope
    provenance, _, blob, _ = diamond(repo, project, depth=3)
    journal = EvaluationJournal(repo, project)
    profile = create_profile(repo, project, model="gpt-6.1-sol", context_limit=100000).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="metadata",
        author_ref=provenance,
        writer_profile_ref=profile,
        reviewer_profile_ref=profile,
    )
    journal.create_run(command, dict(command=command_payload(command)))
    root = journal.prepare(command.run_id, "draft", {}, dict(provenance=provenance.model_dump(mode="json")))
    physical, receipts = [], []
    full = "FULL DURABLE WORKING STATE " * 10000
    for index in range(20):
        request = journal.record(
            dict(
                contract="model-request-v1",
                context=dict(source_tool_turn=dict(turn_index=index)),
                original=provenance.model_dump(mode="json"),
                full=full,
            )
        )
        physical.append(request)
        receipts.append(
            journal.event(
                root,
                "source_tool_result",
                data=dict(
                    turn_index=index,
                    call_index=index,
                    call_id=f"c{index}",
                    name="read_source",
                    turn_request_ref=request.model_dump(mode="json"),
                    result=dict(passage=dict(read_id=f"E{index + 1}", text=full)),
                ),
            )
        )
    fetches = []

    def capture(conn, cursor, statement, parameters, context, many):
        if "SELECT payload FROM runtime_records WHERE" in statement:
            fetches.append(parameters)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        for metadata in journal.event_metadata([root]):
            memo = repo._read_authorization.get().completed
            from podgen.persistence.access_facts import access_facts

            facts = access_facts(repo, repo._read_authorization.get().transaction)
            assert all("payload" not in row for row in facts.runtimes.rows.values())
            assert full not in repr(memo)
            assert full not in repr(metadata)
        # Every common provenance node is loaded once, despite twenty events.
        assert len(fetches) == len(receipts)
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert repo._read_authorization.get() is None
    history = ResearchHistory(journal, root)
    assert len(history.receipts) == 20
    assert full not in repr(vars(history))
    assert history.for_call("source_tool_result", 19)[0]["data"]["result"]["passage"]["text"] == full
    withdraw_blob(repo, project, blob)
    with pytest.raises(AccessDenied):
        history.for_call("source_tool_result", 19)
    with pytest.raises(AccessDenied):
        ResearchHistory(journal, root)
