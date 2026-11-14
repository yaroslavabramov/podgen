"""One bounded immutable descriptor load; no audio bytes or authorization lease are retained."""


def get_many(journal, keys):
    keys = tuple(dict.fromkeys(keys))
    result = {}
    for offset in range(0, len(keys), 64):
        batch = keys[offset : offset + 64]
        result.update(zip(batch, journal.get_many(batch), strict=True))
    return result


def load(journal, cells):
    with journal.read_operation():
        return _load(journal, cells)


def _load(journal, cells):
    attempts = journal.attempt_keys(cells)
    keys = [key for values in attempts.values() for key in values]
    if not keys:
        return {cell["cell_id"]: [] for cell in cells}
    processing = journal.processing_keys(keys)
    values = get_many(
        journal,
        [key + suffix for key in keys for suffix in (":prepared", ":submit", ":receipt", ":audio", ":budget")]
        + [
            key + suffix
            for revisions in processing.values()
            for key in revisions
            for suffix in (":processing", ":result", ":failure", ":decoded")
        ],
    )
    result = {}
    for cell_id, keys in attempts.items():
        rows = []
        for key in keys:
            revisions = [
                dict(
                    key=p,
                    prepared=values.get(p + ":processing"),
                    result=values.get(p + ":result"),
                    failure=values.get(p + ":failure"),
                    decoded=values.get(p + ":decoded"),
                )
                for p in processing[key]
            ]
            prepared = values[key + ":prepared"]
            rows.append(
                dict(
                    take=prepared["take"],
                    key=key,
                    prepared=prepared,
                    intent=values[key + ":submit"],
                    receipt=values[key + ":receipt"],
                    audio=values[key + ":audio"],
                    budget=values[key + ":budget"],
                    result=revisions[-1]["result"],
                    failure=revisions[-1]["failure"],
                    processing=revisions,
                    processing_key=revisions[-1]["key"],
                )
            )
        result[cell_id] = rows
    return result
