"""PostgreSQL work, including nested trigger SQL, for the isolated offline replay."""

from collections import Counter
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

FIELDS = (
    "calls",
    "rows",
    "total_exec_time",
    "shared_blks_hit",
    "shared_blks_read",
    "shared_blks_dirtied",
    "shared_blks_written",
    "temp_blks_read",
    "temp_blks_written",
    "wal_records",
    "wal_bytes",
)


class ServerProbe:
    def __init__(self, url):
        # This connection is not part of the product engine/client SQL probe.
        # Its own queries must not be included in the server measurements either.
        self.engine = create_engine(url, poolclass=NullPool)
        self.connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        if self.connection.exec_driver_sql("SHOW pg_stat_statements.track").scalar_one() != "all":
            self.close()
            raise RuntimeError("The replay requires pg_stat_statements.track=all")
        self.connection.exec_driver_sql("SET pg_stat_statements.track = 'none'")
        self.start = self.snapshot()
        self.phases = {}
        self.completed_operation = None

    def snapshot(self):
        info = dict(self.connection.exec_driver_sql("SELECT * FROM pg_stat_statements_info").mappings().one())
        rows = self.connection.execute(
            text(
                "SELECT userid, queryid, toplevel, query, " + ", ".join(FIELDS) + " FROM pg_stat_statements "
                "WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())"
            )
        ).mappings()
        return info, {(row["userid"], row["queryid"], row["toplevel"]): dict(row) for row in rows}

    @staticmethod
    def difference(before, after):
        old_info, old_rows = before
        info, rows = after
        if info != old_info or not old_rows.keys() <= rows.keys():
            raise RuntimeError("Server SQL statistics reset or evicted during the replay")
        statements = []
        totals = {"top_level": Counter(), "nested": Counter()}
        for key, row in rows.items():
            previous = old_rows.get(key, {})
            delta = {field: row[field] - previous.get(field, 0) for field in FIELDS}
            if any(value < 0 for value in delta.values()):
                raise RuntimeError("Server SQL statistics decreased during the replay")
            if not delta["calls"]:
                continue
            values = {
                field: int(value) if isinstance(value, Decimal) else value for field, value in delta.items()
            }
            totals["top_level" if row["toplevel"] else "nested"].update(values)
            statements.append(
                dict(queryid=str(row["queryid"]), toplevel=row["toplevel"], sql=row["query"], **values)
            )
        return dict(
            totals={kind: dict(values) for kind, values in totals.items()},
            statements=sorted(statements, key=lambda row: row["calls"], reverse=True),
        )

    def finish_phase(self, name, before):
        self.phases.setdefault(name, []).append(self.difference(before, self.snapshot()))

    def report(self):
        return dict(
            scope="Current database only; observer SQL excluded; no statistics reset",
            timing="Top-level execution time includes nested work; do not add the two totals",
            phases="Each phase entry is inclusive; nested phases overlap",
            overall=self.difference(self.start, self.snapshot()),
            completed_operation=self.completed_operation,
            by_phase=self.phases,
        )

    def mark_completed_operation(self):
        self.completed_operation = self.difference(self.start, self.snapshot())

    def close(self):
        self.connection.close()
        self.engine.dispose()
