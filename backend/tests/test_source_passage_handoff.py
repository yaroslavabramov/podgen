"""Selected material carries exact attributed source wording through ordinary import and wire."""

import importlib.util
import json
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials, generation_repo, load_editorial_materials
from podgen.runtime.evaluation.wire_budget import check_visible
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projection_versions import V5, V6, V7, V8, V9
from podgen.writing.projections import CONTRACT, creative_knowledge_view, role_payload
from podgen.writing.reading_views import reading_payload
from podgen.writing.source_passages import card_source_passages

from tests.historical_preview import replay_preview as preview_request
from tests.own_plan_provider import proposal
from tests.test_current_snow_input import source_file
from tests.test_editorial_independence import payload
from tests.test_v5_role_wire import fixture

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "evals/editorial-inputs/current/snow-pump"
RESTORED = CASE / "history/before-percolation-2026-11-08.json"
YORK_CONCLUSION = (
    "no doubt remains upon my\nmind that constant percolation, and for a con-\n"
    "siderable period, had been conveying fluid matter\nfrom the drains into the well"
)
BOOK = ROOT / "evals/cases/development/wp02-batch-01/sources/snow/snow-1855"


def read(path):
    return json.loads(path.read_text())


def current_materials():
    packet = EditorialInput.model_validate_json((CASE / "shared.json").read_bytes())
    return EditorialMaterials(packet, RuntimeRef(record_kind="user_input", record_id=uuid4()), ())


def selected_plan(materials):
    plan = proposal(
        dict(material_catalog=materials.material_catalog, speaker_labels=materials.speaker_labels), "offline"
    )
    selected = [
        card.card_id
        for card in materials.packet.dossier.knowledge_cards
        if card.label in ("Осмотр воды", "Доставка воды", "Осмотр Йорка")
    ]
    plan["plan"]["opening"]["material_refs"] = selected[:1]
    for act in plan["plan"]["acts"]:
        act["material_refs"] = selected
    plan["research_requests"] = []
    return plan, set(selected)


class OriginalText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored -= 1

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def test_full_original_html_matches_the_imported_text_and_all_exact_card_ranges():
    parser = OriginalText()
    parser.feed(BOOK.with_suffix(".html").read_text())
    assert "\n".join(parser.parts) == BOOK.with_suffix(".txt").read_text()
    packet = current_materials().packet
    texts = {}
    for document in packet.model_dump(mode="json")["documents"]:
        raw = source_file(document).read_bytes()
        assert sha256(raw).hexdigest() == document["sha256"]
        texts[document["key"]] = raw.decode()
    for card in packet.dossier.knowledge_cards:
        for quote in card.excerpts:
            assert texts[quote.document][quote.start : quote.end] == quote.text


def test_data_revision_preserves_history_and_identifiers_without_changing_the_brief_or_sources():
    revision = read(CASE / "source-restoration.json")
    previous_path = CASE / revision["previous_input"]
    before = read(previous_path)
    after = read(RESTORED)
    assert sha256(previous_path.read_bytes()).hexdigest() == revision["previous_sha256"]
    assert digest(before) == revision["previous_digest"]
    assert digest(after) == revision["target_digest"]
    assert before["assignment"] == after["assignment"]
    assert [doc for doc in before["documents"] if doc["purpose"] != "dossier"] == [
        doc for doc in after["documents"] if doc["purpose"] != "dossier"
    ]
    old = {card["card_id"]: card for card in before["dossier"]["knowledge_cards"]}
    new = {card["card_id"]: card for card in after["dossier"]["knowledge_cards"]}
    assert set(new) - set(old) == set(revision["added_card_ids"])
    assert set(old) <= set(new)
    assert {key for key in old if old[key] != new[key]} == set(revision["modified_card_ids"])
    for key in old:
        assert old[key]["label"] == new[key]["label"]
    by_label = {card["label"]: card for card in new.values()}
    inspection = by_label["Осмотр воды"]
    assert "другого обстоятельства" in inspection["story_facts"]
    assert "белых хлопьев" in inspection["story_facts"]
    assert "не был установлен" in inspection["story_facts"]
    delivery = by_label["Доставка воды"]
    assert delivery["excerpts"][0]["text"].startswith("Dr. Fraser also first called my attention")
    assert "ни в Уэст-Энде" in delivery["story_facts"]
    assert "пищеварительном" in by_label["Гипотеза передачи"]["story_facts"]


def test_percolation_revision_preserves_the_experimental_packet_and_its_primary_sources():
    revision = read(CASE / "percolation-revision.json")
    assert CASE / revision["previous_input"] == RESTORED
    assert sha256(RESTORED.read_bytes()).hexdigest() == revision["previous_sha256"]
    old = read(RESTORED)
    new = read(CASE / "shared.json")
    assert (
        digest(old) == revision["previous_digest"] == read(CASE / "source-restoration.json")["target_digest"]
    )
    assert digest(new) == revision["target_digest"]
    assert old["assignment"] == new["assignment"]
    for before, after in zip(old["documents"], new["documents"], strict=True):
        assert (before != after) == (before["key"] == "D2")
        for document in (before, after):
            assert sha256(source_file(document).read_bytes()).hexdigest() == document["sha256"]
    changed = []
    for before, after in zip(
        old["dossier"]["knowledge_cards"], new["dossier"]["knowledge_cards"], strict=True
    ):
        assert before["card_id"] == after["card_id"]
        if before != after:
            changed.append(after["card_id"])
    assert changed == revision["modified_card_ids"]
    early_inspection = next(
        card for card in new["dossier"]["knowledge_cards"] if card["label"] == "Ранняя физическая проверка"
    )
    assert early_inspection["excerpts"][1]["text"].endswith("thence into the well itself.")


def test_passages_preserve_full_text_attribution_and_distinct_sources_without_duplicates():
    text = "M1 Cforeign café e\u0301 😀\n" * 3000
    quote = SimpleNamespace(document="a", start=0, end=len(text), text=text)
    another = SimpleNamespace(document="b", start=0, end=len(text), text=text)
    card = SimpleNamespace(excerpts=[quote, quote, another], narrative_excerpt_indices=[0, 1, 2])
    documents = {
        "a": SimpleNamespace(title="First attribution"),
        "b": SimpleNamespace(title="Second attribution"),
    }
    assert card_source_passages(card, documents) == [
        dict(source="First attribution", text=text),
        dict(source="Second attribution", text=text),
    ]
    assert card.excerpts == [quote, quote, another]


@pytest.mark.parametrize("selection", [None, []])
def test_unselected_narrative_excerpts_never_fall_back_to_all_quotes(selection):
    card = SimpleNamespace(excerpts=[SimpleNamespace(document="a", start=0, end=5, text="Whole")])
    if selection is not None:
        card.narrative_excerpt_indices = selection
    assert card_source_passages(card, {"a": SimpleNamespace(title="Attribution")}) == []


@pytest.mark.parametrize("selection", [[0, 0], [-1], [999], [True], [1.0], None])
def test_narrative_selection_rejects_duplicate_invalid_and_ambiguous_indices(selection):
    packet = read(CASE / "shared.json")
    packet["dossier"]["knowledge_cards"][0]["narrative_excerpt_indices"] = selection
    with pytest.raises(ValueError):
        EditorialInput.model_validate(packet)


def test_narrative_selection_allows_empty_or_ordered_choice_without_changing_fact_evidence():
    packet = read(CASE / "shared.json")
    card = next(card for card in packet["dossier"]["knowledge_cards"] if card["label"] == "Возражение врача")
    original = deepcopy(card["excerpts"])
    for selection in ([], [6, 0]):
        card["narrative_excerpt_indices"] = selection
        parsed = EditorialInput.model_validate(packet)
        updated = next(row for row in parsed.dossier.knowledge_cards if row.label == card["label"])
        assert [row.model_dump(mode="json") for row in updated.excerpts] == original
        assert updated.narrative_excerpt_indices == selection


def test_legacy_serialization_omits_new_selector_and_v1_rejects_it():
    revision = read(CASE / "source-restoration.json")
    for path in (CASE / "legacy-shared.json", CASE / revision["previous_input"]):
        before = read(path)
        assert EditorialInput.model_validate(before).model_dump(mode="json") == before
        assert all("narrative_excerpt_indices" not in card for card in before["dossier"]["knowledge_cards"])
    legacy = read(CASE / "legacy-shared.json")
    legacy["dossier"]["knowledge_cards"][0]["narrative_excerpt_indices"] = []
    with pytest.raises(ValueError, match="editorial_narrative_excerpts_forbidden_in_v1"):
        EditorialInput.model_validate(legacy)


def test_curated_sources_remain_separate_from_complete_fact_check_evidence():
    materials = current_materials()
    _, _, _, _, script, *_ = fixture()
    planner = role_payload("plan", materials, projection_version=V9)
    assert sum(len(card.get("source_excerpts", [])) for card in planner["dossier"]["knowledge_cards"]) == 8
    clinical = next(
        card for card in materials.packet.dossier.knowledge_cards if card.label == "Возражение врача"
    )
    creative = creative_knowledge_view(
        materials.packet, labels=False, selected={clinical.card_id}, projection_version=V9
    )
    assert len(clinical.excerpts) == 7 and "source_excerpts" not in creative[0]
    fact = reading_payload("fact-review", materials, script=script, projection_version=V9)
    for quote in clinical.excerpts:
        assert any(row["text"] == quote.text for row in fact["evidence"])
    assert len(fact["evidence"]) == 47
    reader = reading_payload("reader-review", materials, script=script, projection_version=V9)
    assert not {"support", "evidence", "source_excerpts"}.intersection(reader)


@pytest.mark.parametrize("version", [V5, V6, V7, V8])
def test_previous_creative_versions_keep_their_old_material_shape(version):
    materials = current_materials()
    expected = creative_knowledge_view(materials.packet, labels=True)
    assert creative_knowledge_view(materials.packet, labels=True, projection_version=version) == expected
    assert all("source_excerpts" not in card for card in expected)


def assert_selected_sources(support, materials, selected, *, linked=False):
    cards = [card for card in materials.packet.dossier.knowledge_cards if card.card_id in selected]
    documents = {document.key: document for document in materials.packet.documents}
    assert len(support) == len(cards)
    for view, card in zip(support, cards, strict=True):
        expected = card_source_passages(card, documents)
        fields = {"claim", "sources", "source_excerpts"} if expected else {"claim", "sources"}
        if linked:
            fields.add("ref")
            assert view["ref"] == f"M{materials.material_catalog.index(card.card_id) + 1}"
        assert set(view) == fields
        assert view["claim"] == card.story_facts
        assert view.get("source_excerpts", []) == expected


def test_v9_selected_passages_reach_author_and_both_revision_paths_without_unselected_cards():
    materials = current_materials()
    plan, selected = selected_plan(materials)
    _, _, _, _, script, reader, fact, _ = fixture()
    for stage in ("draft", "revise"):
        visible = role_payload(
            stage, materials, approved_plan=plan, draft=script, feedback=reader, projection_version=V9
        )
        assert_selected_sources(visible["support"], materials, selected)
        assert "evidence" not in visible and "necessary_limits" not in visible
    extra = next(card.card_id for card in materials.packet.dossier.knowledge_cards if card.label == "Таблица")
    fact = deepcopy(fact)
    fact["findings"][0]["support_labels"] = [extra]
    visible = reading_payload(
        "revise",
        materials,
        script=script,
        readings=dict(reader=reader, fact=fact),
        decision=dict(recommendation="revise"),
        approved_plan=plan,
        projection_version=V9,
    )
    assert_selected_sources(visible["support"], materials, selected | {extra})
    for field in ("evidence", "repair_evidence", "necessary_limits"):
        assert field not in visible


@pytest.mark.parametrize("archived", [False, True], ids=["current", "frozen-b52d"])
def test_import_helper_and_real_planner_writer_compilation_need_no_provider(
    repo, scope, tmp_path, monkeypatch, archived
):
    from podgen import writing_cli

    project, _ = scope
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(
        writing_cli,
        "OpenAIProvider",
        lambda *args, **kwargs: pytest.fail("import/preview constructed provider"),
    )
    spec = importlib.util.spec_from_file_location("import_snow_case", CASE / "import_case.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    output = tmp_path / "imported"
    assert (
        importer.main(
            [
                "--project",
                str(project),
                "--actor",
                str(repo.actor_id),
                "--objects",
                str(repo.objects.root),
                "--out-dir",
                str(output),
                "--scope",
                "source-restoration-test",
                *(["--input", str(RESTORED)] if archived else []),
            ]
        )
        == 0
    )
    materials = load_editorial_materials(
        generation_repo(repo),
        project,
        RuntimeRef.model_validate(read(output / "shared-ref.json")),
        case_id="snow-pump",
    )
    assert materials.packet.model_dump(mode="json") == read(RESTORED if archived else CASE / "shared.json")
    for document, original in zip(materials.documents, materials.packet.documents, strict=True):
        assert document.text == source_file(original.model_dump(mode="json")).read_text()
    plan, selected = selected_plan(materials)
    profile = fixture()[3]
    for stage, options in (("plan", {}), ("draft", dict(approved_plan=plan))):
        body = preview_request(uuid4(), stage, profile, materials, **options)
        check_visible(body, CONTRACT)
        visible = payload(body)
        if stage == "plan":
            cards = visible["dossier"]["knowledge_cards"]
            assert len(cards) == len(materials.material_catalog)
            assert all(set(card) <= {"claim", "sources", "source_excerpts", "card_id"} for card in cards)
            for view, card in zip(cards, materials.packet.dossier.knowledge_cards, strict=True):
                assert view.get("source_excerpts", []) == [
                    dict(
                        source=next(
                            doc.title for doc in materials.packet.documents if doc.key == quote.document
                        ),
                        text=quote.text,
                    )
                    for quote in (card.excerpts[index] for index in card.narrative_excerpt_indices or [])
                ]
        else:
            assert_selected_sources(visible["support"], materials, selected, linked=True)
            passages = [e["text"] for card in visible["support"] for e in card.get("source_excerpts", [])]
            assert (YORK_CONCLUSION in passages) == (not archived)
    script = fixture()[4]
    fact = payload(preview_request(uuid4(), "fact-review", profile, materials, draft=script))
    excerpts = [e["text"] for e in fact["evidence"]]
    assert (YORK_CONCLUSION in excerpts) == (not archived)
    assert any(e.endswith("thence into the well itself.") for e in excerpts) == (not archived)
