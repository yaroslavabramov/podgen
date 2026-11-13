"""Pinned v6 uses the full existing read budget; v5 and source text remain exact."""

from types import SimpleNamespace

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.source_tool_contract import create, from_manifest
from podgen.writing.source_tools import SourceCorpus
from tests.test_frozen_source_tools import materials as source_materials


def test_read_source_default_is_explicitly_pinned_not_inferred_from_maximum():
    text = "ёx" * 8000
    corpus = SourceCorpus(source_materials(text))
    args = dict(source_id="S1", start=0, end=None)
    old = corpus.execute("read_source", args, read_id="E1", max_read_chars=9600)
    new = corpus.execute("read_source", args, read_id="E1", max_read_chars=9600, default_read_chars=9600)
    assert old["passage"]["text"] == text[:2400] and old["next_start"] == 2400
    assert new["passage"]["text"] == text[:9600] and new["next_start"] == 9600
    last = corpus.execute(
        "read_source",
        dict(args, start=new["next_start"]),
        read_id="E2",
        max_read_chars=9600,
        default_read_chars=9600,
    )
    assert new["passage"]["text"] + last["passage"]["text"] == text


def test_contract_v5_stays_valid_and_v6_default_cannot_be_changed_silently():
    materials = SimpleNamespace(packet=SimpleNamespace(contract="research-editorial-input/1"))
    new = create(SimpleNamespace(name="production/1"), materials)
    assert (
        new["version"] == "frozen-corpus-tools/6"
        and new["default_read_chars"] == new["max_read_chars"] == 9600
    )
    old = {k: v for k, v in new.items() if k != "default_read_chars"}
    old["version"] = "frozen-corpus-tools/5"
    for contract in (old, new):
        assert from_manifest(dict(behavior_profile="production/1", source_tool_contract=contract)) == contract
    with pytest.raises(ContractError):
        from_manifest(
            dict(behavior_profile="production/1", source_tool_contract=dict(new, default_read_chars=2400))
        )


def test_saved_longitude_report_sections_take_five_v6_reads_instead_of_eleven():
    from pathlib import Path

    from podgen.research.materials.sections import partition
    from podgen.writing.research_materials.tools import read

    root = Path(__file__).resolve().parents[2]
    text = (root / "evals/reports/2026-11-13-hosted-research/report.md").read_text()
    sections = {f"M{i}": section for i, section in enumerate(partition(text), 1)}
    corpus = SimpleNamespace(research_sections=sections, research_texts={"report": text}, research_sources={})
    counts = []
    for default in (2400, 9600):
        calls = 0
        for alias in ("M6", "M5", "M7", "M8", "M3"):
            cursor, parts = 0, []
            while cursor is not None:
                result = read(
                    corpus,
                    dict(material_id=alias, start=cursor, end=None, context=True),
                    read_id="E1",
                    max_read_chars=9600,
                    default_read_chars=default,
                )
                parts.append(result["material_passage"]["text"])
                assert len(parts[-1]) + sum(len(piece["text"]) for piece in result["context"]) <= 9600
                cursor = result["next_start"]
                calls += 1
            section = sections[alias]
            assert "".join(parts) == text[section.start : section.end]
        counts.append(calls)
    assert counts == [11, 5]
