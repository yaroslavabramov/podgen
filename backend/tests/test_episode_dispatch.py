"""Dispatch pause through real ModelStep publication and cold recovery."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from podgen.episodes.production.control import ProductionPaused
from podgen.runtime.dispatch import before_external, bind
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.model_turns import TurnJournal
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.research_context import ResearchContext
from podgen.runtime.models.research_history import ResearchHistory
from tests.test_model_step_publication import run, setup


@pytest.mark.parametrize("stop_at", ["model_count", "model_submit", "after_submit"])
def test_pause_keeps_received_count_and_response_and_never_repeats_paid_work(repo, scope, stop_at):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    calls = dict(count=0, submit=0)
    paused = False

    def count(body):
        nonlocal paused
        calls["count"] += 1
        paused = stop_at == "model_submit"
        return WireReply(b'{"input_tokens":100}')

    def submit(body, **options):
        nonlocal paused
        calls["submit"] += 1
        paused = stop_at == "after_submit"
        return WireReply(
            json.dumps(
                dict(
                    id="resp_pause",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    def check(kind, identity):
        if paused or kind == stop_at:
            raise ProductionPaused()

    provider = SimpleNamespace(count=count, submit=submit)
    gateway = ModelGateway(tj, lambda _: provider)
    with bind(check):
        if stop_at == "after_submit":
            assert (
                run(
                    gateway,
                    turn,
                    profile,
                    limits,
                    prepared_phase=phase,
                    request_snapshot=snapshot,
                    manifest_snapshot=manifest,
                )["usage"]["output_tokens"]
                == 10
            )
            with pytest.raises(ProductionPaused):
                before_external("next_action", "next")
        else:
            with pytest.raises(ProductionPaused):
                run(
                    gateway,
                    turn,
                    profile,
                    limits,
                    prepared_phase=phase,
                    request_snapshot=snapshot,
                    manifest_snapshot=manifest,
                )
    fresh = EvaluationJournal(repo, scope[0])
    events = fresh.events(root)
    kinds = [event["event_kind"] for event in events]
    assert calls == dict(
        count=0 if stop_at == "model_count" else 1, submit=1 if stop_at == "after_submit" else 0
    )
    if stop_at == "model_submit":
        assert kinds.count("count_response") == 1 and "submit_intent" not in kinds
        current_root = fresh.invocation(root["run_id"], "draft")
        physical = TurnJournal(fresh, current_root, 0)
        current = physical._row()
        assert current["status"] == "counted" and current["input_tokens"] == 100
        assert (
            run(
                ModelGateway(physical, lambda _: provider),
                current,
                profile,
                limits,
                manifest_snapshot=manifest,
            )["usage"]["output_tokens"]
            == 10
        )
        assert calls == dict(count=1, submit=1)
    if stop_at == "after_submit":
        assert kinds.count("provider_response") == 1


def test_context_rotation_captures_pending_count_before_pausing_next_count(repo, scope):
    journal, root, _, _, _, snapshot, _, profile, _, _ = setup(repo, scope)
    calls = 0

    def count(body):
        nonlocal calls
        calls += 1
        return WireReply(b'{"input_tokens":100}')

    def pause_second(kind, identity):
        if calls:
            raise ProductionPaused()

    provider = SimpleNamespace(count=count)
    context = ResearchContext(
        ModelGateway(journal, lambda _: provider),
        root,
        profile,
        snapshot[0],
        ResearchHistory(journal, root),
        lambda: None,
    )
    second = deepcopy(snapshot[0])
    second["input"] = [dict(role="user", content="A different candidate")]
    with bind(pause_second):
        assert context.measured(snapshot[0], 0, "normal") == 100
        with pytest.raises(ProductionPaused):
            context.measured(second, 0, "checkpoint")
    assert calls == 1
    fresh = EvaluationJournal(repo, scope[0])
    current_root = fresh.invocation(root["run_id"], "draft")
    kinds = [event["event_kind"] for event in fresh.events(current_root)]
    assert kinds == ["source_context_count_intent", "source_context_count"]
    restored = ResearchContext(
        ModelGateway(fresh, lambda _: provider),
        current_root,
        profile,
        snapshot[0],
        ResearchHistory(fresh, current_root),
        lambda: None,
    )
    assert restored.measured(snapshot[0], 0, "normal") == 100 and calls == 1
