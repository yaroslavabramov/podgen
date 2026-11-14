import json
from contextlib import contextmanager

from sqlalchemy import create_engine, text


def json_text(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)

    @contextmanager
    def read_transaction(self):
        # One authorization sees one committed snapshot. A concurrent withdrawal applies to the next read.
        # Never reuse the snapshot/memo for a later provider count/submit or another public operation.
        with self.engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield connection

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection


def query(connection, sql, **params):
    return connection.execute(text(sql), params)


def one(connection, sql, **params):
    return query(connection, sql, **params).mappings().first()


def bounded_rows(connection, sql, identities, **params):
    """Read a caller-owned finite identity set in bounded SQL parameter batches."""
    identities = tuple(dict.fromkeys(identities))
    for offset in range(0, len(identities), 64):
        yield from query(connection, sql, ids=list(identities[offset : offset + 64]), **params).mappings()
