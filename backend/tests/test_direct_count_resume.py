"""Direct counted steps bind native input and configuration across cold recovery."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.model_turns import TurnJournal
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.count_receipts import native_count
from podgen.runtime.models.gateway import ModelGateway
from tests.test_model_step_publication import run, setup


def test_crash_during_count_blocks_ordinary_replay_before_another_provider_call(repo, scope):
    _, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    counts = []

    def interrupted(body):
        counts.append(body)
        raise RuntimeError("worker stopped during count")

    provider = SimpleNamespace(count=interrupted, submit=lambda *a, **kw: pytest.fail("unexpected submit"))
    with pytest.raises(RuntimeError, match="worker stopped during count"):
        run(
            ModelGateway(tj, lambda _: provider),
            turn,
            profile,
            limits,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )

    fresh = EvaluationJournal(repo, scope[0])
    parent = fresh.invocation(root["run_id"], "draft")
    resumed = TurnJournal(fresh, parent, 0)
    with pytest.raises(Conflict, match="private_unknown_count_requires_authorized_recovery"):
        run(ModelGateway(resumed, lambda _: provider), resumed._row(), profile, limits)
    assert len(counts) == 1
    kinds = [event["event_kind"] for event in fresh.events(parent)]
    assert kinds.count("count_intent") == 1
    assert "count_response" not in kinds and "submit_intent" not in kinds


@pytest.mark.parametrize("changed", [None, "input", "output_limit", "adapter_version"])
def test_direct_count_cold_resume_reuses_only_exact_request_and_configuration(repo, scope, changed):
    journal, root, tj, turn, phase, snapshot, manifest, profile, limits, _ = setup(repo, scope)
    calls = dict(count=0, submit=0)

    def count(body):
        calls["count"] += 1
        return WireReply(
            b'{"input_tokens":100}',
            request_body=json.dumps(native_count(body, profile.provider)).encode(),
        )

    def submit(body, **options):
        calls["submit"] += 1
        return WireReply(
            json.dumps(
                dict(
                    id="resp_direct",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[],
                    usage=dict(input_tokens=100, output_tokens=10),
                )
            ).encode()
        )

    checks = 0

    class Interrupted(Exception):
        pass

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise Interrupted()

    provider = SimpleNamespace(count=count, submit=submit)
    with pytest.raises(Interrupted):
        ModelGateway(tj, lambda _: provider).invoke_once(
            turn,
            profile,
            limits=limits,
            totals=dict(input=0, output=0),
            guard=guard,
            full_response=True,
            prepared_phase=phase,
            request_snapshot=snapshot,
            manifest_snapshot=manifest,
        )
    assert calls == dict(count=1, submit=0)
    fresh = EvaluationJournal(repo, scope[0])
    current_root = fresh.invocation(root["run_id"], "draft")
    physical = TurnJournal(fresh, current_root, 0)
    current = physical._row()
    assert current["status"] == "counted"
    body = deepcopy(snapshot[0])
    if changed == "input":
        body["input"] = [dict(role="user", content="A different task")]
    elif changed == "output_limit":
        profile.output_limit += 1
    elif changed == "adapter_version":
        profile.adapter_version = "different-adapter"
    options = dict(
        request_snapshot=(
            body,
            fresh.read(RuntimeRef(record_kind="tool_execution", record_id=current["request_record_id"])),
        ),
        manifest_snapshot=manifest,
    )
    gateway = ModelGateway(physical, lambda _: provider)
    if changed:
        with pytest.raises(Conflict, match="count_receipt_request_changed"):
            run(gateway, current, profile, limits, **options)
        assert calls == dict(count=1, submit=0)
    else:
        response = run(gateway, current, profile, limits, **options)
        assert response["usage"] == dict(input_tokens=100, output_tokens=10)
        assert calls == dict(count=1, submit=1)
