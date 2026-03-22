from app.config import settings
from app.pipeline.graph import _should_revolt


def test_should_revolt_uses_rewrite_loop_counter_only_when_rewrite_requested() -> None:
    assert _should_revolt(
        {
            "quality_signals": {"revolt_triggered": True},
            "iteration_counts": {"writer_planner_loops": 0},
        }
    ) == "write_screenplay"

    assert _should_revolt(
        {
            "quality_signals": {"revolt_triggered": False},
            "iteration_counts": {"writer_planner_loops": 99},
        }
    ) == "audit_citations"


def test_should_revolt_stops_rewrite_loop_at_configured_limit() -> None:
    assert _should_revolt(
        {
            "quality_signals": {"revolt_triggered": True},
            "iteration_counts": {"writer_planner_loops": settings.max_writer_rewrite_loops},
        }
    ) == "audit_citations"


def test_should_revolt_allows_rewrite_below_configured_limit() -> None:
    assert _should_revolt(
        {
            "quality_signals": {"revolt_triggered": True},
            "iteration_counts": {"writer_planner_loops": settings.max_writer_rewrite_loops - 1},
        }
    ) == "write_screenplay"
