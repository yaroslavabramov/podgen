"""Original selectors, pre-cap copy grouping, guarded unions and versioned alias pages."""

import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from podgen.domain.errors import Conflict
from podgen.persistence.database import query
from podgen.retrieval.config import PASSAGE_POLICY, STREAMING_PROFILE, profile, streaming_profile
from podgen.retrieval.diversity import group
from podgen.retrieval.preview import page
from podgen.retrieval.streaming import StreamingSearch
from podgen.runtime.evaluation.source_search_index import PostgresSearchIndex
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from podgen.writing.research_materials.runtime_tools import source_definitions
from podgen.writing.source_tools.contracts import definitions
from podgen.writing.source_tools.corpus import Source
from tests.source_search_support import TinyModels
from tests.test_source_search_index import admitted


def document(text):
    return Source("Original source", text, hashlib.sha256(text.encode()).hexdigest())


def row(source, start, end):
    return dict(
        source_id=source,
        start=start,
        end=end,
        read_start=max(0, start - 200),
        rank=1,
        aliases=[dict(source_id=source, start=start, end=end)],
    )


def bounds(text, first, last):
    tokens = list(re.finditer(r"\S+", text))
    return tokens[first].start(), tokens[last - 1].end()


@pytest.mark.parametrize("replacement", ["£650", "never", "Jones"])
def test_similar_passages_do_not_suppress_distinct_numbers_negations_or_attribution(replacement):
    original = " ".join(["Background"] * 5 + ["£500", "not", "Smith"] + [f"term{i}" for i in range(100)])
    changed = original.replace({"£650": "£500", "never": "not", "Jones": "Smith"}[replacement], replacement)
    docs = {"S1": document(original), "S2": document(changed)}
    rows = [row("S1", 0, len(original)), row("S2", 0, len(changed))]
    result = group(rows, docs)
    assert len(result) == 1
    output = page(result, docs, "evidence", 0, profile())
    assert output["hits"][0]["support_relation"] == "overlapping_text_variants"
    versions = output["hits"][0]["variants"][0]["versions"]
    assert [v["text"] for v in versions] == [
        {"£650": "£500", "never": "not", "Jones": "Smith"}[replacement],
        replacement,
    ]
    assert output["hits"][0]["aliases"] == [r["aliases"][0] for r in rows]


@pytest.mark.parametrize("whitespace", [" ", " \n  "])
def test_shifted_windows_merge_only_as_a_lossless_original_union(whitespace):
    words = [f"term{i}" for i in range(420)]
    left = " ".join(words)
    right = whitespace.join(words)
    a, b = bounds(left, 50, 274), bounds(right, 80, 304)
    original = [row("S1", *a), row("S2", *b)]
    docs = {"S1": document(left), "S2": document(right)}
    result = group(original, docs)
    assert len(result) == 1
    merged = result[0]
    assert (merged["start"], merged["end"]) == bounds(left, 50, 304)
    assert merged["aliases"] == [
        dict(source_id="S1", start=a[0], end=a[1]),
        dict(source_id="S2", start=b[0], end=b[1]),
    ]
    assert merged["support_relation"] == "shared_original_text"
    for alias in merged["aliases"]:
        assert docs[alias["source_id"]].text[alias["start"] : alias["end"]]


def test_ordinary_adjacent_windows_keep_their_large_distinct_parts():
    text = " ".join(f"term{i}" for i in range(420))
    result = group(
        [row("S1", *bounds(text, 0, 224)), row("S1", *bounds(text, 192, 416))], {"S1": document(text)}
    )
    assert len(result) == 2


def test_many_aliases_have_bounded_pages_and_preserve_every_original_selector(monkeypatch):
    from podgen.retrieval import alias_pages, preview

    scopes = []
    original = preview.read_sources

    @contextmanager
    def bounded_scope(documents):
        documents = list(documents)
        scopes.append(len(documents))
        with original(documents):
            yield

    monkeypatch.setattr(preview, "read_sources", bounded_scope)
    monkeypatch.setattr(alias_pages, "read_sources", bounded_scope)
    text = "Original evidence passage " * 30
    docs = {f"S{i}": document(text) for i in range(100)}
    aliases = [dict(source_id=key, start=0, end=len(text)) for key in docs]
    rows = [dict(row("S0", 0, len(text)), aliases=aliases, support_relation="shared_original_text")]
    first = page(rows, docs, "evidence", 0, profile())
    hit = first["hits"][0]
    seen = list(hit["aliases"])
    action = hit["next_alias_page"]["arguments"]
    assert hit["alias_count"] == 100 and hit["independent_confirmation"] is False
    while action["offset"] is not None:
        result = page(
            rows,
            docs,
            action["query"],
            action["offset"],
            profile(),
            aliases_of=action["aliases_of"],
            within=action["within"],
        )
        assert (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            <= profile()["max_response_bytes"]
        )
        seen.extend(result["aliases"])
        action = dict(action, offset=result["next_offset"])
    assert seen == aliases
    assert scopes[0] == 2 and max(scopes) <= profile()["page_hits"]


@pytest.mark.parametrize(
    "addition",
    [
        "(large)",
        "(small)",
        "(not large)",
        "(1865)",
        "(Smith)",
        "(by smith)",
        "(five)",
        "(пять)",
        "(only)",
        "(smith)",
        "(reported)",
    ],
)
def test_every_parenthetical_difference_is_visible_with_both_original_contexts(addition):
    words = [f"term{i}" for i in range(420)]
    left = " ".join(words)
    right = " ".join(words[:120] + [addition] + words[120:])
    docs = {"S1": document(left), "S2": document(right)}
    rows = [row("S1", *bounds(left, 50, 274)), row("S2", *bounds(right, 80, 305))]
    result = group(rows, docs)
    assert len(result) == 1
    merged = result[0]
    assert merged["support_relation"] == "overlapping_text_variants"
    versions = merged["variants"][0]["versions"]
    assert [v["text"] for v in versions] == ["", addition]
    assert {v["source_id"] for v in versions} == {"S1", "S2"}
    for version in versions:
        assert docs[version["source_id"]].text[version["start"] : version["end"]] == version["text"]
        context = version["context"]
        assert docs[version["source_id"]].text[context["start"] : context["end"]] == context["text"]
        assert len(context["text"].split()) > 5
    output = page(result, docs, "term150", 0, profile())
    assert output["hits"][0]["variants"] == merged["variants"]
    assert output["hits"][0]["independent_confirmation"] is False
    assert output["hits"][0]["aliases"] == [r["aliases"][0] for r in rows]


def test_large_diff_or_many_diffs_keep_original_hits_without_truncation():
    words = [f"term{i}" for i in range(420)]
    left = " ".join(words)
    variants = [
        " ".join(words[:120] + ["x" * 1100] + words[120:]),
        " ".join(word + (" changed" if i in (90, 140, 190) else "") for i, word in enumerate(words)),
    ]
    for right in variants:
        docs = {"S1": document(left), "S2": document(right)}
        result = group([row("S1", *bounds(left, 50, 274)), row("S2", *bounds(right, 80, 305))], docs)
        assert len(result) == 2
        assert not any(r.get("variants") for r in result)


def test_preview_checks_access_for_visible_variant_sources_beyond_inline_aliases(monkeypatch):
    from podgen.retrieval import preview

    docs = {f"S{i}": document("original passage " * 30) for i in range(3)}
    original = preview.read_sources
    scopes = []

    @contextmanager
    def checked(documents):
        documents = list(documents)
        scopes.append(set(id(d) for d in documents))
        with original(documents):
            yield

    monkeypatch.setattr(preview, "read_sources", checked)
    selected = row("S0", 0, len(docs["S0"].text))
    selected.update(
        aliases=[dict(source_id=key, start=0, end=len(doc.text)) for key, doc in docs.items()],
        support_relation="overlapping_text_variants",
        variants=[dict(versions=[dict(source_id="S2", start=0, end=8, text="original")])],
    )
    output = page([selected], docs, "original", 0, profile())
    assert scopes == [set(id(d) for d in docs.values())]
    assert set(output["sources"]) == set(docs)


@pytest.mark.parametrize("persistent", [False, True])
def test_new_tool_schema_preserves_complete_existing_field_instructions(persistent):
    old = definitions(retrieval=True)
    new = definitions(retrieval=True, retrieval_policy=PASSAGE_POLICY)
    if persistent:
        old = source_definitions(old)
        new = source_definitions(new, retrieval_policy=PASSAGE_POLICY)
    before = next(t for t in old if t["name"] == "search_sources")["parameters"]
    after = next(t for t in new if t["name"] == "search_sources")["parameters"]
    for key, value in before["properties"].items():
        assert after["properties"][key] == value
    assert "any supported language" in after["properties"]["query"]["description"]
    assert "aliases_of" in after["required"] and "default" not in after["properties"]["aliases_of"]


def test_exact_sql_groups_before_every_candidate_cap_and_keeps_all_aliases(repo, scope):
    journal, ref = admitted(repo, scope[0])
    index = PostgresSearchIndex(journal, ref, profile())
    fragment = "needle precise original evidence. "
    raw = fragment * 100
    first = document(raw)
    second = document("needle different useful evidence £65.")
    punctuation = document("needle precise original evidence!")
    ranges = [(i * len(fragment), (i + 1) * len(fragment)) for i in range(100)]
    vector = np.eye(1, 384, dtype=np.float32)[0]
    index.add_document("S1", first, ranges, "en", np.tile(vector, (100, 1)))
    index.add_document("S2", second, [(0, len(second.text))], "en", np.tile(vector, (1, 1)))
    index.add_document("S3", punctuation, [(0, len(punctuation.text))], "en", np.tile(vector, (1, 1)))
    old = index.candidates("needle", vector, {"S1", "S2", "S3"}, 3)
    new = index.candidates("needle", vector, {"S1", "S2", "S3"}, 3, group_exact=True)
    assert all(len(branch) == 3 for branch in new.values())
    assert {c.source_id for c, _ in old["dense"]} == {"S1"}
    for branch in new.values():
        assert {candidate[0].source_id for candidate in branch} == {"S1", "S2", "S3"}
        assert sorted(len(candidate[2]) for candidate in branch) == [1, 1, 100]
        for _, _, aliases in branch:
            assert (
                len(
                    {
                        ({"S1": raw, "S2": second.text, "S3": punctuation.text}[c.source_id])[c.start : c.end]
                        for c in aliases
                    }
                )
                == 1
            )


def test_policy_switch_reuses_vectors_and_rejects_cross_policy_result_versions(repo, scope):
    journal, ref = admitted(repo, scope[0])
    run = uuid4()
    network = TinyModels()
    docs = {"S1": document("Original needle evidence. " * 80)}
    old = StreamingSearch(
        docs,
        {},
        deepcopy(STREAMING_PROFILE),
        index=StreamingSearchIndex(journal, ref, deepcopy(STREAMING_PROFILE), run_id=run),
        network=network,
    )
    args = SimpleNamespace(query="needle", within=None, offset=0, result_version=None)
    old_page = old.search(args)
    emitted = network.embedded
    new = StreamingSearch(
        docs,
        {},
        streaming_profile(),
        index=StreamingSearchIndex(journal, ref, streaming_profile(), run_id=run),
        network=network,
    )
    assert network.embedded == emitted
    new_page = new.search(args)
    assert new_page["result_version"] != old_page["result_version"]
    for search, foreign in ((old, new_page), (new, old_page)):
        with pytest.raises(Conflict, match="source_search_result_version_mismatch"):
            search.search(SimpleNamespace(**dict(vars(args), result_version=foreign["result_version"])))
    assert (
        old.search(SimpleNamespace(**dict(vars(args), result_version=old_page["result_version"]))) == old_page
    )


def test_fingerprint_backfill_accepts_an_identical_concurrent_writer(repo, scope, monkeypatch):
    from podgen.runtime.evaluation import passage_index

    journal, ref = admitted(repo, scope[0])
    index = PostgresSearchIndex(journal, ref, profile())
    doc = document("Original needle evidence.")
    vector = np.eye(1, 384, dtype=np.float32)
    index.add_document("S1", doc, [(0, len(doc.text))], "en", vector)
    with repo.database.transaction() as tx:
        query(tx, "UPDATE source_search_chunks SET passage_sha256=NULL WHERE project_id=:p", p=scope[0])
    original = passage_index.read_sources

    @contextmanager
    def concurrent(documents):
        with original(documents):
            yield
        with repo.database.transaction() as tx:
            query(
                tx,
                "UPDATE source_search_chunks SET passage_sha256=:hash WHERE project_id=:p",
                hash=hashlib.sha256(doc.text.encode()).hexdigest(),
                p=scope[0],
            )

    monkeypatch.setattr(passage_index, "read_sources", concurrent)
    index.prepare_passages({"S1": doc})
    assert index.candidates("needle", vector[0], {"S1"}, 2, group_exact=True)["dense"]
