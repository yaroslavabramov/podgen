"""One SQL statement for a bounded, internally described batch of rows."""

import re

from .database import query


def insert_rows(tx, table, rows, *, json_columns=(), on_conflict=False, returning=False):
    if not rows:
        return
    if len(rows) > 64:
        collected = []
        for offset in range(0, len(rows), 64):
            result = insert_rows(
                tx,
                table,
                rows[offset : offset + 64],
                json_columns=json_columns,
                on_conflict=on_conflict,
                returning=returning,
            )
            if returning:
                collected.extend(result)
        return collected if returning else None
    columns = tuple(rows[0])
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", table) or any(
        not re.fullmatch(r"[a-z_][a-z_0-9]*", column) for column in columns
    ):
        raise ValueError("invalid_internal_sql_identifier")
    parameters, values = {}, []
    for index, row in enumerate(rows):
        if tuple(row) != columns:
            raise ValueError("inconsistent_batch_columns")
        fields = []
        for column, value in row.items():
            key = f"{column}_{index}"
            parameters[key] = value
            fields.append(f"CAST(:{key} AS json)" if column in json_columns else f":{key}")
        values.append("(" + ",".join(fields) + ")")
    suffix = " ON CONFLICT DO NOTHING" if on_conflict else ""
    suffix += " RETURNING *" if returning else ""
    result = query(
        tx, f"INSERT INTO {table} ({','.join(columns)}) VALUES {','.join(values)}{suffix}", **parameters
    )
    return list(result.mappings()) if returning else None
