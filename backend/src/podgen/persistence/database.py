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
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection


def query(connection, sql, **params):
    return connection.execute(text(sql), params)


def one(connection, sql, **params):
    return query(connection, sql, **params).mappings().first()
