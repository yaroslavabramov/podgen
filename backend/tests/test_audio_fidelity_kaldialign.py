import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.audio.fidelity.alignment import COMPOUND_RULE, INTEGER_RULE, align, provenance
from podgen.audio.fidelity.context import expected_range
from podgen.audio.fidelity.contracts import CandidateReport, CandidateReportV5, validate_report
from podgen.audio.fidelity.tokens import expected_tokens, tokens
from podgen.runtime.evaluation.audio.fidelity.processing import revisions
from podgen.runtime.evaluation.audio.fidelity.service import reprocess, run
from tests.test_audio_fidelity_candidates import recognition, report
from tests.test_audio_fidelity_evaluation import ASR, setup


@pytest.mark.parametrize(
    "left,right",
    [
        ("по тому", "потому"),
        ("не было", "небыло"),
        ("белая бумага", "белаябумага"),
        ("а б в", "абв"),
        ("ПО ТОМУ", "потому"),
    ],
)
def test_alphabetic_compounds_are_reversible_spelling_equivalence(left, right):
    for source, recognized in ((left, right), (right, left)):
        expected, actual = tokens(source), tokens(recognized)
        opcodes, equivalents = align(expected, actual)
        assert all(row[0] in ("equal", "equivalent") for row in opcodes)
        assert equivalents[0]["rule"] == COMPOUND_RULE
        assert equivalents[0]["expected_tokens"] == expected
        assert equivalents[0]["recognized_tokens"] == actual
        assert equivalents[0]["expected_token_range"] == [0, len(expected)]
        assert all(source[t["char_start"] : t["char_end"]] == t["text"] for t in expected)


@pytest.mark.parametrize(
    "left,right",
    [
        ("1 2", "12"),
        ("2 3", "23"),
        ("0.5", "05"),
        ("12", "1 2"),
        ("3 5", "35"),
        ("3.5", "35"),
        ("−12", "12"),
        ("±12", "12"),
        ("12 %", "12"),
        ("не работает", "работает"),
        ("Сноу", "снова"),
        ("главная подозреваемая", "главное подозреваемое"),
        ("1854", "1855"),
        ("пятьсот тридцать пять", "536"),
    ],
)
def test_values_symbols_negation_and_name_changes_remain_candidates(left, right):
    result = report(left, recognition(right))
    assert any(c["kind"] == "script_discrepancy" for c in result["candidates"])
    assert result["alignment"] == provenance()
    CandidateReportV5.model_validate(result)


def test_compound_offsets_preserve_neighboring_deletion_and_numeric_equivalence():
    source = "По тому не было двадцать три человека."
    actual = "Потому было 23 человека."
    left = expected_tokens([dict(utterance_id="source-id", text=source)])
    right = tokens(actual)
    opcodes, equivalents = align(left, right)
    assert ("equivalent", 0, 2, 0, 1) in opcodes
    assert ("delete", 2, 3, 1, 1) in opcodes
    assert {entry["rule"] for entry in equivalents} == {COMPOUND_RULE, INTEGER_RULE}
    assert expected_range(opcodes[:1], 0, 1, len(left)) == (0, 2)
    assert expected_range(opcodes, 0, 1, len(left)) == (0, 3)  # Includes adjacent omitted "не".
    numeric = next(entry for entry in equivalents if entry["rule"] == INTEGER_RULE)
    assert numeric["value"] == 23
    assert numeric["expected_token_range"] == [4, 6]
    assert numeric["expected_tokens"][0]["utterance_id"] == "source-id"


def test_kaldi_edit_path_retains_repeated_word_deletion():
    opcodes, _ = align(tokens("не не работает"), tokens("не работает"))
    assert sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "delete") == 1


@pytest.mark.parametrize(
    "left,right", [("по тому НЕ работает", "потому работает"), ("потому НЕ работает", "по тому работает")]
)
def test_candidate_source_selectors_follow_compound_raw_ranges(left, right):
    result = report(left, recognition(right))
    candidate = next(c for c in result["candidates"] if c["kind"] == "script_discrepancy")
    assert candidate["operation"] == "delete"
    assert [t["text"] for t in candidate["expected_tokens"]] == ["НЕ"]
    selector = candidate["script_selectors"][0]
    assert left[selector["char_start"] : selector["char_end"]] == "НЕ"


def test_historical_schema_and_report_shape_are_unchanged():
    path = (
        Path(__file__).resolve().parents[2] / "docs/podcast-production/audio/fidelity-candidates.schema.json"
    )
    assert json.loads(path.read_text()) == CandidateReport.model_json_schema()
    current = report("слово", recognition("слово"))
    legacy = deepcopy(current)
    legacy.pop("alignment")
    legacy["contract"] = "literal-fidelity-candidates/4"
    assert validate_report(legacy).model_dump(mode="json") == CandidateReport.model_validate(
        legacy
    ).model_dump(mode="json")
    assert isinstance(validate_report(current), CandidateReportV5)


def test_resume_preserves_historical_result_and_reprocess_versions_new_engine(monkeypatch):
    journal, config = setup(monkeypatch)
    transport = ASR()
    assert run(journal, config, transport) == "candidates_generated"
    previous = revisions(journal, config.audit_id)[-1]
    legacy_key = str(journal.ref(previous["key"] + ":result").record_id)
    # Simulate an already-persisted pre-upgrade report, without rebuilding its request.
    legacy = journal.records[legacy_key]["report"]
    legacy.pop("alignment")
    legacy["contract"] = "literal-fidelity-candidates/4"
    frozen = deepcopy(journal.records)
    assert run(journal, config, transport) == "candidates_generated" and len(transport.calls) == 1
    assert journal.records == frozen
    assert reprocess(journal, config, "Explicit offline kaldialign revision") == "candidates_generated"
    assert all(journal.records[key] == value for key, value in frozen.items())
    newest = revisions(journal, config.audit_id)[-1]
    assert newest["prepared"]["alignment"] == provenance()
    assert newest["result"]["report"]["contract"] == "literal-fidelity-candidates/6"
    assert len(transport.calls) == 1
