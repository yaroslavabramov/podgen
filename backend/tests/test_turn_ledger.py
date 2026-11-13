"""Committed turn accounting is incremental; cold history is read exactly once."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.turn_ledger import TurnLedger


@pytest.mark.parametrize("width", [1, 4096])
def test_new_turn_accounting_never_enumerates_historical_rows_or_submitted_set(monkeypatch, width):
    reads = []
    rows = [
        dict(
            turn_index=index,
            status="response_completed",
            provider_response_id=f"resp_{index}",
            input_tokens=100,
            output_tokens=10,
        )
        for index in range(width)
    ]

    def load(*args):
        reads.append(1)
        return rows

    history = SimpleNamespace(
        submitted_turns=lambda: frozenset(range(width)), is_submitted=lambda index: True
    )
    monkeypatch.setattr(model_turns, "rows", load)
    ledger = TurnLedger(None, {}, 1000, history)
    assert len(reads) == 1
    history.submitted_turns = lambda: pytest.fail("must use one-turn membership")
    monkeypatch.setattr(model_turns, "rows", lambda *args: pytest.fail("must use committed rows"))
    assert ledger.before(width) == dict(input=100 * width, output=10 * width)
    for status, output in (("submitting", None), ("response_completed", 10)):
        ledger.committed(
            dict(
                turn_index=width,
                invocation_id=uuid4(),
                status=status,
                provider_response_id="resp_new",
                input_tokens=100,
                output_tokens=output,
            )
        )
    assert ledger.usage() == dict(
        input=100 * (width + 1), output=10 * (width + 1), known_output=10 * (width + 1), unknown=False
    )
    assert ledger.before(width + 1) == dict(input=100 * (width + 1), output=10 * (width + 1))


def test_refusal_invalidates_prefix_before_return(monkeypatch):
    row = dict(
        turn_index=0, status="submitting", provider_response_id=None, input_tokens=100, output_tokens=None
    )
    monkeypatch.setattr(model_turns, "rows", lambda *args: [row])
    history = SimpleNamespace(submitted_turns=lambda: {0}, is_submitted=lambda index: True)
    ledger = TurnLedger(None, {}, 1000, history)
    assert ledger.before(1) == dict(input=100, output=1000)
    monkeypatch.setattr(model_turns, "charged_rows", lambda *args, **kwargs: [])
    ledger.committed(dict(row, status="failed", input_tokens=90))
    assert ledger.before(1) == dict(input=90, output=1000)
    assert ledger.usage() == dict(input=0, output=0, known_output=0, unknown=False)


def test_projection_is_pure_before_refusal_receipt_is_committed(monkeypatch):
    row = dict(
        turn_index=0, status="submitting", provider_response_id=None, input_tokens=100, output_tokens=None
    )
    monkeypatch.setattr(model_turns, "rows", lambda *args: [row])
    history = SimpleNamespace(submitted_turns=lambda: {0}, is_submitted=lambda index: True)
    ledger = TurnLedger(None, {}, 1000, history)
    monkeypatch.setattr(
        model_turns, "charged_rows", lambda *args, **kwargs: pytest.fail("I/O inside projection")
    )
    assert ledger.projected(dict(row, status="failed")) == dict(
        input=100, output=1000, known_output=0, unknown=True
    )
