"""Read saved event bodies in bounded batches without changing order or meaning."""

from sqlalchemy import event

from podgen.runtime.evaluation.model_turns import events
from tests.test_model_step_publication import setup


def test_saved_event_reads_preserve_full_payload_order_in_bounded_batches(repo, scope):
    journal, root, *_ = setup(repo, scope)
    text = "SYNTHETIC полное неизменённое содержание 🛰️ " * 1000
    expected = []
    for start in range(0, 129, 64):
        phase = journal.publication(root)
        for index in range(start, min(start + 64, 129)):
            ref = phase.event("synthetic_receipt", data=dict(index=index, full=text))
            expected.append(ref.record_id)
        phase.commit()
    statements = []

    def capture(conn, cursor, sql, params, context, many):
        statements.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        records = events(journal, root, kinds={"synthetic_receipt"})
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)
    assert [record["_record_id"] for record in records] == expected
    assert [record["data"]["index"] for record in records] == list(range(129))
    assert all(record["data"]["full"] == text for record in records)
    assert sum("SELECT record_id,payload FROM runtime_records" in sql for sql in statements) == 3
    assert sum(sql.startswith("WITH nodes(record_id)") for sql in statements) == 3
    assert not any(sql.startswith("SELECT payload FROM runtime_records") for sql in statements)
