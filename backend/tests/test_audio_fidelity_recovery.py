import json
from copy import deepcopy

import pytest

from podgen.audio.http import HttpReceipt
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.fidelity import processing
from podgen.runtime.evaluation.audio.fidelity.deliveries import deliveries
from podgen.runtime.evaluation.audio.fidelity.export import export
from podgen.runtime.evaluation.audio.fidelity.service import reprocess, run
from tests.test_audio_fidelity_candidates import recognition, report
from tests.test_audio_fidelity_evaluation import ASR, setup


def test_explicit_retry_recovers_old_one_request_manifest_and_preserves_wire(monkeypatch):
    j, c = setup(monkeypatch)
    refused = ASR(HttpReceipt(503, {}, b"service unavailable", 5))
    assert run(j, c, refused) == "provider_rejected"
    frozen = deepcopy(j.records)
    recovered = ASR()
    assert run(j, c, recovered) == "provider_rejected" and not recovered.calls
    assert (
        run(
            j,
            c,
            recovered,
            retry_policy=dict(reason="Provider recovered", max_total_requests=2, max_total_cost_usd="0.04"),
        )
        == "candidates_generated"
    )
    assert all(j.records[k] == v for k, v in frozen.items())
    assert recovered.calls[0][1] == refused.calls[0][1]
    assert len(deliveries(j, c)) == 2 and c.max_requests == 1
    assert run(j, c, recovered) == "candidates_generated" and len(recovered.calls) == 1


def test_retry_rejects_unknown_and_enforces_total_reservations(monkeypatch):
    j, c = setup(monkeypatch)
    unknown = ASR(HttpReceipt(200, {}, b"partial", 5, False, "ReadTimeout"))
    run(j, c, unknown)
    with pytest.raises(ContractError):
        run(j, c, ASR(), retry_policy=dict(reason="retry", max_total_requests=2, max_total_cost_usd="1"))
    assert len(deliveries(j, c)) == 1
    j, c = setup(monkeypatch)
    rejected = ASR(HttpReceipt(503, {}, b"rejected", 5))
    run(j, c, rejected)
    assert (
        run(
            j, c, rejected, retry_policy=dict(reason="retry", max_total_requests=2, max_total_cost_usd="0.02")
        )
        == "budget_exhausted"
    )
    assert len(rejected.calls) == 1


def test_failed_reprocess_exposes_current_failure_and_immutable_last_success(monkeypatch, tmp_path):
    j, c = setup(monkeypatch)
    run(j, c, ASR())
    first = export(j, c, tmp_path)
    success = first["current"]["current_result"]
    archived = tmp_path / success["path"]
    original = archived.read_bytes()
    mtime = archived.stat().st_mtime_ns
    monkeypatch.setattr(
        processing, "generate", lambda *a, **k: (_ for _ in ()).throw(ContractError("injected_failure"))
    )
    assert reprocess(j, c, "Explicit local recalculation") == "response_invalid"
    current = export(j, c, tmp_path)
    assert current["candidates"] == 0 and current["current"]["current_result"] is None
    assert current["current"]["last_successful_result"] == success
    assert json.loads((tmp_path / "candidates.json").read_text())["report"] is None
    assert archived.read_bytes() == original and archived.stat().st_mtime_ns == mtime


@pytest.mark.parametrize(
    "words,digits",
    [
        ("пятьсот тридцать пять", "535"),
        ("одна тысяча двести сорок два", "1242"),
        ("два миллиона триста тысяч пятьсот", "2300500"),
        ("два человека", "2 человека"),
    ],
)
def test_unambiguous_integer_surfaces_preserve_raw_spans(words, digits):
    result = report(words, recognition(digits))
    assert not [c for c in result["candidates"] if c["kind"] == "script_discrepancy"]
    assert result["normalization_equivalences"]
    item = result["normalization_equivalences"][0]
    assert item["expected_tokens"][0]["text"] in words
    assert all(t["char_end"] > t["char_start"] for t in item["expected_tokens"])


@pytest.mark.parametrize(
    "words,digits",
    [
        ("один сказал", "1 сказал"),
        ("сто, двадцать", "120"),
        ("один два", "12"),
        ("три целых пять десятых", "35"),
        ("−12", "12"),
        ("двадцать три недели", "23 дня"),
        ("не двадцать три", "23"),
        ("пятьсот тридцать пять", "536"),
    ],
)
def test_ambiguous_and_changed_values_units_negation_remain_candidates(words, digits):
    assert report(words, recognition(digits))["candidates"]


@pytest.mark.parametrize(
    "words,digits",
    [
        ("двадцать ноль", "20"),
        ("тысяча восемьсот пятьдесят четвертом", "1850 четвертом"),
        ("сто\nдвадцать", "120"),
        ("два тысяч", "2000"),
    ],
)
def test_incomplete_or_malformed_number_grammar_is_not_collapsed(words, digits):
    assert report(words, recognition(digits))["candidates"]


@pytest.mark.parametrize(
    "words,digits",
    [
        ("двухсот тридцати", "230"),
        ("восьмисот девяноста шести", "896"),
        ("тридцать первого августа", "31 августа"),
        ("третьего сентября", "3 сентября"),
    ],
)
def test_explicit_genitive_counts_and_calendar_days(words, digits):
    assert not [
        c for c in report(words, recognition(digits))["candidates"] if c["kind"] == "script_discrepancy"
    ]


@pytest.mark.parametrize(
    "words,digits",
    [
        ("миллион тысяча", "1001000"),
        ("сто ноль", "100"),
        ("пятьсот тридцати пяти", "535"),
        ("двадцати первого августа", "21 августа"),
        ("первого человека", "1 человека"),
    ],
)
def test_ambiguous_scale_or_ordinal_context_stays_literal(words, digits):
    assert report(words, recognition(digits))["candidates"]


@pytest.mark.parametrize("words,digits", [("один тысяча", "1000"), ("две миллиона", "2000000")])
def test_malformed_scale_gender_remains_for_audio_review(words, digits):
    assert report(words, recognition(digits))["candidates"]


@pytest.mark.parametrize("words,digits", [("тысяча ноль", "1000"), ("миллион ноль", "1000000")])
def test_trailing_zero_after_scale_remains_uncertain(words, digits):
    assert report(words, recognition(digits))["candidates"]
