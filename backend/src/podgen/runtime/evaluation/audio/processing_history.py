"""Explicit processing-history view over the bounded descriptor reader."""

from .history_snapshot import get_many


def runs(journal, key):
    keys = journal.processing_keys([key])[key]
    values = get_many(
        journal, [value + suffix for value in keys for suffix in (":processing", ":result", ":failure")]
    )
    return [
        dict(
            key=value,
            prepared=values[value + ":processing"],
            result=values[value + ":result"],
            failure=values[value + ":failure"],
        )
        for value in keys
    ]
