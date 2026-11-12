"""Lossless decision projection; display scores never classify a word."""

GROUPS = ("review", "uncertain", "no_correction", "other", "unassessed")


def summarize(response):
    groups = {name: [] for name in GROUPS}
    for index, original in enumerate((response or {}).get("words") or []):
        row = original if isinstance(original, dict) else {}
        decision = row.get("decision")
        unassessed = (
            response.get("assessable") is False
            or row.get("assessed") is False
            or decision in ("unassessable", "partial")
            or ("score" in row and row["score"] is None)
        )
        group = "unassessed" if unassessed else decision if decision in GROUPS[:3] else "other"
        groups[group].append(
            dict(
                index=index,
                word=row.get("word", row.get("text")) if isinstance(original, dict) else original,
                decision=decision,
                score=row.get("score"),
                assessed=row.get("assessed"),
                omitted=row.get("omitted"),
            )
        )
    return dict(
        contract="toneperfect-word-decision-summary/1",
        available=response is not None,
        counts={name: len(rows) for name, rows in groups.items()},
        groups=groups,
        qualification_status="not_evaluated",
        production_ready=False,
    )
