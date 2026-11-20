"""Observable shared-order reuse with fresh authorization and run-local X names."""

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.materials import targeted
from podgen.research.materials.admissions import admit_report
from podgen.research.materials.orders import KEY, VERSION
from podgen.research.materials.resolver import ResearchMaterialResolver
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.source_acquisition_support import ARTICLE
from tests.test_research_material_resolver import fixture, save_report


def request():
    anchors = [dict(utterance_index=1, quote="one and the same"), dict(utterance_index=0, quote="Before.")]
    script = dict(utterances=[dict(speaker="A", text=t) for t in ["Before.", "one and the same", "After."]])
    args = dict(
        question="Verify the comparison.",
        reason="Check the actual statement.",
        anchors=anchors,
        research_contract="targeted-script-context/1",
        **{KEY: VERSION},
        script_context=dict(
            script_hash=digest(script),
            anchors=anchors,
            utterances=[dict(utterance_index=i, **row) for i, row in enumerate(script["utterances"])],
        ),
    )
    return args


@pytest.mark.parametrize("resolved", [False, True])
def test_one_local_result_is_reused_without_report_parse_or_document_read(
    repo, scope, producer, monkeypatch, resolved
):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    repo.objects = MeasuredObjects(repo.objects.root)
    measured = Measurements(repo.database, repo.objects)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    args = request()
    anchors = args["anchors"]
    run, first_call, second_call = uuid4(), uuid4(), uuid4()
    first = resolver.resolve(
        "request_research", args, operation_id=first_call, run_id=run, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(first.result["child_run_id"]))
    body = dict(
        answer="The source was inspected." if resolved else "No answer established.",
        limitations="" if resolved else "No original evidence available.",
        evidence=[dict(url="https://example.com/article", quote=ARTICLE[:80], relation="Original account.")]
        if resolved
        else [],
    )
    save_report(child, json.dumps(body))
    counts = dict(parse=0, validate=0, report_read=0, object_read=0, object_stream=0, guard=0)
    parse, validate, read = targeted.parse, targeted.validated_excerpts, ResearchJournal.bytes
    object_read, stream = repo.objects.read, repo.objects.iter_read

    def counted_parse(text):
        counts["parse"] += 1
        return parse(text)

    def counted_validate(*a, **kw):
        counts["validate"] += 1
        return validate(*a, **kw)

    def counted_read(self, key, blob):
        if self.run_id == child.run_id and key == "result":
            counts["report_read"] += 1
        return read(self, key, blob)

    def guard():
        counts["guard"] += 1

    monkeypatch.setattr(targeted, "parse", counted_parse)
    monkeypatch.setattr(targeted, "validated_excerpts", counted_validate)
    monkeypatch.setattr(ResearchJournal, "bytes", counted_read)
    with measured.phase("first_materialization"):
        result = resolver.resolve("request_research", args, operation_id=first_call, run_id=run, guard=guard)
    assert result.result["status"] == ("resolved" if resolved else "unresolved")
    before = counts.copy()
    repeated = deepcopy(args)
    repeated["reason"] = "The final review still asks this question."
    repeated["anchors"] = [anchors[1], anchors[0], anchors[0]]
    repeated["script_context"]["anchors"] = repeated["anchors"]

    def counted_object(*a, **kw):
        counts["object_read"] += 1
        return object_read(*a, **kw)

    def counted_stream(*a, **kw):
        counts["object_stream"] += 1
        yield from stream(*a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(repo.objects, "read", counted_object)
        patch.setattr(repo.objects, "iter_read", counted_stream)
        with measured.phase("repeat_same_order"):
            again = resolver.resolve(
                "request_research", repeated, operation_id=second_call, run_id=run, guard=guard
            )
    assert again == result
    assert counts["guard"] > before["guard"]
    assert {k: counts[k] for k in ("parse", "validate", "report_read")} == {
        k: before[k] for k in ("parse", "validate", "report_read")
    }
    assert counts["parse"] == counts["report_read"] == 1
    assert counts["object_read"] == counts["object_stream"] == 0
    assert len(fetcher.calls) == 1
    destination = os.environ.get("PODGEN_BOUND_RESEARCH_MEASUREMENTS")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        value = dict(
            measured.snapshot(),
            status=result.result["status"],
            repeat_report_parses=counts["parse"] - before["parse"],
            repeat_excerpt_validations=counts["validate"] - before["validate"],
            repeat_explicit_report_reads=counts["report_read"] - before["report_read"],
            repeat_network_calls=0,
            paid_calls=0,
        )
        (directory / (result.result["status"] + ".json")).write_text(json.dumps(value, indent=2) + "\n")
    store = resolver.store(run)
    identity = result.result["research_order_id"]
    key = "material-research-order:" + identity + ":result"
    for call in (first_call, second_call):
        receipt = store.get("material-request:" + str(call) + ":order-result:" + digest(key))
        assert receipt["result_ref"] == store.ref(key).model_dump(mode="json")
    assert store.get("material-request:" + str(second_call) + ":request")["arguments"] == repeated

    def denied():
        raise AccessDenied()

    with pytest.raises(AccessDenied):
        resolver.resolve("request_research", repeated, operation_id=second_call, run_id=run, guard=denied)
    # A different run interprets the retained report once, using its own names.
    other = uuid4()
    resolver.restore(run_id=other, guard=guard)
    unrelated = ResearchJournal(repo, scope[0], uuid4())
    save_report(unrelated, "# Unrelated saved synthesis")
    assert admit_report(resolver.store(other), materials, unrelated, "Other", []).source_id == "X1"
    local = resolver.resolve("request_research", repeated, operation_id=uuid4(), run_id=other, guard=guard)
    assert local.result["research_order_id"] == identity
    assert counts["parse"] == counts["report_read"] == 2
    if resolved:
        assert result.result["report_id"] == "X1" and local.result["report_id"] == "X2"
    changed = deepcopy(repeated)
    changed["question"] = "A distinct unanswered question."
    distinct = resolver.resolve("request_research", changed, operation_id=uuid4(), run_id=run, guard=guard)
    assert distinct.result["research_order_id"] != identity
    assert distinct.result["child_run_id"] != str(child.run_id)
    assert len(fetcher.calls) == 1


def test_concurrent_writing_runs_prepare_only_one_research_commission(repo, scope, producer, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from podgen.domain.errors import Conflict

    materials, _, acquire, _ = fixture(repo, scope, producer, monkeypatch)

    def prepare(_):
        resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
        run, call = uuid4(), uuid4()
        args = request()
        try:
            result = resolver.resolve(
                "request_research", args, operation_id=call, run_id=run, guard=lambda: None
            )
            return resolver, run, call, result
        except Conflict as error:
            assert str(error) == "hosted_research_run_busy"
            return resolver, run, call, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = list(pool.map(prepare, range(2)))
    results = []
    for resolver, run, call, result in calls:
        if result is None:
            result = resolver.resolve(
                "request_research", request(), operation_id=call, run_id=run, guard=lambda: None
            )
        results.append(result.result)
    assert len({r["research_order_id"] for r in results}) == 1
    assert len({r["child_run_id"] for r in results}) == 1
    child = ResearchJournal(repo, scope[0], UUID(results[0]["child_run_id"]))
    assert child.get("manifest") and child.get("approval") is None


def test_shared_unresolved_result_is_reparsed_once_locally_without_new_commission(
    repo, scope, producer, monkeypatch
):
    from podgen.research.materials import order_resolution
    from podgen.research.materials.targeted_reparse import VERSION as VALIDATION_VERSION

    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run = uuid4()
    pending = resolver.resolve(
        "request_research", request(), operation_id=uuid4(), run_id=run, guard=lambda: None
    )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    text = json.dumps(
        dict(
            answer="The original account.",
            limitations="",
            evidence=[dict(url="https://example.com/article", quote=ARTICLE[:80], relation="The record.")],
        )
    )
    save_report(child, text + "\n\n**Sources:**\n1. [Document](https://example.com/article)\n")
    raw = child.get("result")
    key = "material-research-order:" + pending.result["research_order_id"]
    original = dict(
        status="unresolved",
        error="targeted_research_invalid_answer",
        research_order_id=pending.result["research_order_id"],
    )
    store = resolver.store(run)
    store.put(key + ":result", original)

    def no_recommission(*a, **kw):
        pytest.fail("A saved paid report must be reinterpreted, not recommissioned")

    monkeypatch.setattr(order_resolution, "resolve_research", no_recommission)
    result = resolver.resolve(
        "request_research", request(), operation_id=uuid4(), run_id=run, guard=lambda: None
    )
    assert result.result["status"] == "resolved"
    assert store.get(key + ":result") == original
    derived = store.get(key + ":validation:" + VALIDATION_VERSION + ":result")
    assert derived["original_result_ref"] == store.ref(key + ":result").model_dump(mode="json")

    def no_parse(*a, **kw):
        pytest.fail("The shared interpretation was already persisted")

    monkeypatch.setattr(targeted, "parse", no_parse)
    again = resolver.resolve(
        "request_research", request(), operation_id=uuid4(), run_id=run, guard=lambda: None
    )
    assert again == result and child.get("result") == raw
    assert len(fetcher.calls) == 1
