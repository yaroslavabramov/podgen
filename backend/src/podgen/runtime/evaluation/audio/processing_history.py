"""Independent local processing revisions of one paid synthesis receipt."""


def runs(journal, key):
    values = [
        dict(
            key=key,
            prepared=journal.get(key + ":processing"),
            result=journal.get(key + ":result"),
            failure=journal.get(key + ":failure"),
        )
    ]
    number = 1
    while journal.get(f"{key}:reprocess:{number}:processing") is not None:
        revision = f"{key}:reprocess:{number}"
        values.append(
            dict(
                key=revision,
                prepared=journal.get(revision + ":processing"),
                result=journal.get(revision + ":result"),
                failure=journal.get(revision + ":failure"),
            )
        )
        number += 1
    return values
