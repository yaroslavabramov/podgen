from app.pipeline.phase3_research.librarian_fast import _build_targeted_query


def test_build_targeted_query_does_not_append_asset_tags() -> None:
    question = "How does the HKD issuance mechanism work?"
    missing_assets = ["CASE", "ANECDOTE", "STATISTIC"]
    assert _build_targeted_query(question, missing_assets) == question
