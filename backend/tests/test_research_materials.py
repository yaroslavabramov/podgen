"""Exact report ranges, source numbering, access and honest evidence boundaries."""

from types import SimpleNamespace

import pytest

from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.database import query
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.research.materials.sections import partition, source_mapping
from podgen.runtime.evaluation.source_tool_contract import applies, create, from_manifest
from podgen.writing.research_materials.pinning import KEY, pin, validate
from podgen.writing.research_materials.views import planning_view, selected_sections
from podgen.writing.source_tools import SourceCorpus, resolve_review_reads
from tests.research_material_cases import REPORT, saved


def load(repo, project, ref):
    return load_editorial_materials(generation_repo(repo), project, ref, case_id="hosted-case")


def test_partition_keeps_full_unicode_and_uses_final_source_numbers():
    mapping = source_mapping(REPORT)
    sections = partition(
        REPORT, sources={n: dict(url=row["url"], source_id=f"S{n}") for n, row in mapping.items()}
    )
    assert "".join(REPORT[s.start : s.end] for s in sections) == REPORT
    assert sections[0].occurrences[0].source_id == "S11"
    assert sections[1].occurrences[0].source_id == "S2"
    fenced = "Intro\n```md\n## not a section\n```\n## actual\nBody\n"
    assert len(partition(fenced)) == 2


@pytest.mark.parametrize("fallback", [False, True])
def test_admission_catalog_paging_and_original_evidence(repo, scope, producer, monkeypatch, fallback):
    ref, config, acquisition, article = saved(repo, scope, producer, monkeypatch, fallback=fallback)
    m = load(repo, scope[0], ref)
    assert m.packet.research_assignment["research_focus"] == ["Not mandatory for writing"]
    view = planning_view(m)
    assert set(view) == {"research_contents", "source_catalog"}
    assert [r["source_id"] for r in view["source_catalog"]] == ["S2", "S11"]
    assert [r["status"] for r in view["source_catalog"]] == ["not_acquired", "available"]
    assert view["source_catalog"][1]["retained_version"] == ("jina" if fallback else "original")
    assert m.packet.sources[1].original_occurrences[0]["start"] == 99999
    corpus = SourceCorpus(m)
    assert (
        corpus.execute("read_source", dict(source_id="S2", start=0, end=None), read_id="E1")["error"]
        == "source_not_readable"
    )
    cursor, parts = 0, []
    while cursor is not None:
        result = corpus.execute(
            "read_material",
            dict(material_id="M1", start=cursor, end=None, context=True),
            read_id="E3",
            max_read_chars=2400,
        )
        passage = result["material_passage"]
        assert passage["read_id"] == "R3" and "passage" not in result
        assert len(passage["text"]) + sum(len(c["text"]) for c in result["context"]) <= 2400
        assert passage["text"] == REPORT[passage["report_start"] : passage["report_end"]]
        assert result["reported_sources"] == [dict(source_id="S11", citation_number=11, status="available")]
        parts.append(passage["text"])
        cursor = result["next_start"]
    assert "".join(parts) == REPORT[m.packet.sections[0].start : m.packet.sections[0].end]
    chosen = selected_sections(m, {"section-001"})
    assert chosen[0]["text"] == "".join(parts) and "claim" not in chosen[0]
    assert article not in str(chosen)
    original = corpus.execute("read_source", dict(source_id="S11", start=0, end=150), read_id="E2")["passage"]
    assert original["text"] == article[:150]
    with pytest.raises(ContractError):
        resolve_review_reads(dict(findings=[dict(source_read_ids=["R3"])]), [original], m)
    contract = create(SimpleNamespace(name="production/1"), m)
    assert from_manifest(dict(behavior_profile="production/1", source_tool_contract=contract)) == contract
    assert all(
        applies(stage, contract)
        for stage in (
            "plan",
            "plan-review",
            "plan-revise",
            "plan-review-revised",
            "draft",
            "fact-review-2",
            "revise-2",
        )
    )
    old = create(SimpleNamespace(name="production/1"))
    assert old["version"] == "frozen-corpus-tools/4" and not applies("plan", old)
    manifest = {KEY: pin(m), "source_tool_contract": contract}
    validate(m, manifest)
    with pytest.raises(ContractError):
        validate(m, {})
    assert load(repo, scope[0], ref).as_payload() == m.as_payload()
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        corpus.execute(
            "read_material", dict(material_id="M1", start=0, end=None, context=False), read_id="E1"
        )


def test_unavailable_override_cannot_leave_a_readable_document(repo, scope, producer, monkeypatch):
    ref, *_ = saved(repo, scope, producer, monkeypatch, unavailable=True)
    m = load(repo, scope[0], ref)
    assert len(m.documents) == 1 and m.packet.sources[1].document is None
    assert not SourceCorpus(m).documents


def test_report_reads_reauthorize_source_blob_withdrawal(repo, scope, producer, monkeypatch):
    from podgen.domain.contracts.common import BlobRef
    from tests.authorization_helpers import withdraw_blob

    ref, _, acquisition, _ = saved(repo, scope, producer, monkeypatch)
    materials = load(repo, scope[0], ref)
    corpus = SourceCorpus(materials)
    representation = acquisition.sources[0].representation_refs[0]
    value = repo.get(scope[0], representation).payload
    withdraw_blob(repo, scope[0], BlobRef.model_validate(value["canonical_text_blob"]))
    with pytest.raises(AccessDenied):
        corpus.execute(
            "read_material", dict(material_id="M1", start=0, end=None, context=False), read_id="E1"
        )


def test_report_tool_receipt_replays_without_reading_again(repo, scope, producer, monkeypatch):
    import json

    from podgen.domain.errors import Conflict
    from podgen.runtime.evaluation import model_turns
    from podgen.runtime.models.tool_gateway import _execute
    from tests.source_tool_runtime_support import MemoryJournal

    ref, *_ = saved(repo, scope, producer, monkeypatch)
    materials = load(repo, scope[0], ref)
    corpus = SourceCorpus(materials)
    contract = create(SimpleNamespace(name="production/1"), materials)
    journal = MemoryJournal({}, {})
    history = model_turns.EventHistory(journal, journal.root)
    call = dict(
        call_id="material-1",
        name="read_material",
        arguments=json.dumps(dict(material_id="M1", start=0, end=4000, context=True)),
    )
    result = _execute(journal, journal.root, corpus, contract, call, 0, 0, lambda: None, history)
    assert result["material_passage"]["read_id"] == "R1"
    monkeypatch.setattr(corpus, "execute", lambda *a, **k: pytest.fail("A frozen read was executed twice"))
    assert _execute(journal, journal.root, corpus, contract, call, 0, 0, lambda: None, history) == result
    with pytest.raises(Conflict):
        _execute(
            journal, journal.root, corpus, contract, dict(call, arguments="{}"), 0, 0, lambda: None, history
        )
