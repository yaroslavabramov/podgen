"""Shared immutable paid-attempt and local-processing history layout."""


def revisions(journal, key):
    rows = []
    number = 1
    while journal.get(f"{key}:processing:{number}") is not None:
        where = f"{key}:processing:{number}"
        rows.append(
            dict(
                key=where,
                prepared=journal.get(where),
                result=journal.get(where + ":result"),
                failure=journal.get(where + ":failure"),
            )
        )
        number += 1
    return rows


def attempts(journal, key):
    rows = []
    number = 1
    while journal.get(f"{key}:attempt:{number}:prepared") is not None:
        where = f"{key}:attempt:{number}"
        rows.append(
            dict(
                key=where,
                number=number,
                prepared=journal.get(where + ":prepared"),
                intent=journal.get(where + ":submit"),
                receipt=journal.get(where + ":receipt"),
                processing=revisions(journal, where),
            )
        )
        number += 1
    return rows
