"""Stable card selection through ordinary admission/run/export, with no provider network."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from podgen import editorial_tools, writing_cli
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import import_shared
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.contracts import WireReply
from podgen.writing.card_authoring import add_card, rebind_fixed, upgrade_fixed, upgrade_shared
from podgen.writing.card_catalog import catalog
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.editorial_schema import packet_schema
from podgen.writing.model_references import CATALOG_KEY, ModelReferences
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_independent_readings import ReadingProvider
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


def legacy():
    return json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/shared.json").read_text())


def test_creation_and_renaming_never_recompute_ids_and_keep_both_names(tmp_path):
    old = legacy()
    packet, mapping = upgrade_shared(old)
    for before, after in zip(
        old["dossier"]["knowledge_cards"], packet["dossier"]["knowledge_cards"], strict=True
    ):
        assert before["label"] == after["label"] and before["title"] == after["title"]
        assert after["card_id"] == mapping["card_ids"][before["label"]]
        assert after["excerpts"] == before["excerpts"]
    identities = set(catalog(EditorialInput.model_validate(packet)))
    changed = deepcopy(packet)
    changed["dossier"]["knowledge_cards"].reverse()
    for card in changed["dossier"]["knowledge_cards"]:
        card.update(label="Одинаковое имя", title="Одинаковый длинный заголовок")
    assert set(catalog(EditorialInput.model_validate(changed))) == identities
    raw = deepcopy(changed["dossier"]["knowledge_cards"][0])
    raw.pop("card_id")
    added, identity = add_card(changed, raw)
    assert identity not in identities
    assert set(catalog(EditorialInput.model_validate(added))) == identities | {identity}
    source = tmp_path / "old.json"
    source.write_text(json.dumps(old, ensure_ascii=False))
    target = tmp_path / "same.json"
    with pytest.raises(ValueError, match="output_paths_must_differ"):
        editorial_tools.main(
            ["upgrade", "--input", str(source), "--out", str(target), "--map-out", str(target)]
        )
    assert not target.exists()
    out, path = tmp_path / "packet.json", tmp_path / "map.json"
    args = ["upgrade", "--input", str(source), "--out", str(out), "--map-out", str(path)]
    assert editorial_tools.main(args) == 0
    saved = out.read_bytes()
    with pytest.raises(FileExistsError):
        editorial_tools.main(args)
    assert out.read_bytes() == saved and json.loads(source.read_text()) == old


@pytest.mark.parametrize(
    "version,field,value",
    [
        (1, "card_id", None),
        (1, "card_id", "C01"),
        (1, "label", None),
        (2, "card_id", None),
        (2, "card_id", 17),
        (2, "label", None),
        (2, "unpublished_identity", "C01"),
    ],
)
def test_schema_and_actual_admission_reject_null_and_extra_identity_fields(
    repo, scope, version, field, value
):
    data = legacy() if version == 1 else upgrade_shared(legacy())[0]
    data["dossier"]["knowledge_cards"][0][field] = value
    definition = packet_schema(version=version)["$defs"]["KnowledgeCard"]
    if field in definition["properties"]:
        assert definition["properties"][field]["type"] == "string"
        assert field in definition["required"]
    else:
        assert definition["additionalProperties"] is False
    # Empty source map proves rejection happens before reading or persisting any sources.
    with pytest.raises(ValidationError):
        import_shared(repo, scope[0], data, {})


@pytest.mark.parametrize("fault", ["duplicate", "unknown_summary", "unknown_thread", "missing"])
def test_identity_referential_integrity_never_resolves_display_names(fault):
    data, _ = upgrade_shared(legacy())
    cards = data["dossier"]["knowledge_cards"]
    if fault == "duplicate":
        cards[1]["card_id"] = cards[0]["card_id"]
    elif fault == "missing":
        cards[0].pop("card_id")
    elif fault == "unknown_summary":
        data["dossier"]["summary_support_labels"] = [cards[0]["label"]]
    else:
        data["dossier"]["threads"] = [
            dict(question="Вопрос", explanation="Объяснение", support_labels=["Cforeign"], limitations=[])
        ]
    with pytest.raises(ValidationError):
        EditorialInput.model_validate(data)


def test_fixed_rebind_allows_only_names_and_card_order_of_exact_previous_version():
    data, _ = upgrade_shared(legacy())
    old = json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/fixed-plan.json").read_text())
    fixed = upgrade_fixed(old, data)
    altered = deepcopy(data)
    altered["dossier"]["knowledge_cards"][0]["claim"] += " Другой факт."
    with pytest.raises(ContractError, match="migration_binding_mismatch"):
        upgrade_fixed(old, altered)
    changed = deepcopy(data)
    changed["dossier"]["knowledge_cards"].reverse()
    for card in changed["dossier"]["knowledge_cards"]:
        card.update(label="Совпадение", title="Совпадение")
    rebound = rebind_fixed(fixed, data, changed)
    assert rebound["plan"] == fixed["plan"] and rebound["document"] == fixed["document"]
    assert rebound["shared_input_digest"] == digest(
        EditorialInput.model_validate(changed).model_dump(mode="json")
    )
    bad = deepcopy(changed)
    bad["dossier"]["knowledge_cards"][0]["claim"] += " Другой факт."
    with pytest.raises(ContractError, match="only_card_names_and_order"):
        rebind_fixed(fixed, data, bad)
    with pytest.raises(ContractError, match="matching_previous"):
        rebind_fixed(fixed, changed, data)
    foreign = deepcopy(fixed)
    foreign["plan"]["plan"]["acts"][0]["material_refs"] = ["Cforeign"]
    with pytest.raises(ValueError, match="unknown_material_locator"):
        rebind_fixed(foreign, data, changed)


class CardProvider(ReadingProvider):
    def __init__(self, chosen, correction, *, bad_plan=False, bad_fact=False):
        super().__init__(("revise", "keep"))
        self.chosen, self.correction, self.bad_plan, self.bad_fact = chosen, correction, bad_plan, bad_fact

    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        stage = body["metadata"]["podgen_stage"]
        response = json.loads(reply.body)
        content = response["output"][0]["content"][0]
        output = json.loads(content["text"])
        if stage == "plan":
            refs = ["Cforeign" if self.bad_plan else self.chosen]
            output["plan"]["opening"]["material_refs"] = refs
            for act in output["plan"]["acts"]:
                act["material_refs"] = refs
            self.upstream.plan = deepcopy(output)
        if stage == "draft":
            output["utterances"].append(
                dict(speaker=output["utterances"][0]["speaker"], text="Вторая конкретная мысль для проверки.")
            )
            self.upstream.script = deepcopy(output)
        if stage == "fact-review":
            first = output["findings"][0]
            first["support_labels"] = ["Cforeign" if self.bad_fact else self.correction]
            second = deepcopy(first)
            second["support_labels"] = [self.chosen]
            second["anchors"] = [
                dict(utterance_index=1, quote=payload(body)["script"]["utterances"][1]["text"])
            ]
            second["observation"] = "Второе независимое замечание."
            output["findings"].append(second)
        content["text"] = json.dumps(output, ensure_ascii=False)
        return WireReply(json.dumps(response, ensure_ascii=False).encode())


def admitted(data, own_dataset, tmp_path, monkeypatch):
    prefix, common, own, mapping, _ = prepare(own_dataset, "snow-pump", tmp_path, monkeypatch)
    original, _ = upgrade_shared(data)
    packet = deepcopy(original)
    chosen = packet["dossier"]["knowledge_cards"][0]["card_id"]
    correction = packet["dossier"]["knowledge_cards"][1]["card_id"]
    packet["dossier"]["knowledge_cards"].reverse()
    for card in packet["dossier"]["knowledge_cards"][:]:
        if card["card_id"] in (chosen, correction):
            card.update(label="Два одинаковых имени", title="Два одинаковых заголовка")
    path, ref = tmp_path / "v2.json", tmp_path / "v2-ref.json"
    raw = (json.dumps(packet, ensure_ascii=False, indent=3) + "\n").encode()
    path.write_bytes(raw)
    assert invoke(prefix, "editorial-import", input=path, source_map=mapping, out=ref) == 0
    again = tmp_path / "v2-again-ref.json"
    assert invoke(prefix, "editorial-import", input=path, source_map=mapping, out=again) == 0
    assert again.read_bytes() == ref.read_bytes()
    for command in (common, own):
        command.update(author_ref="@" + str(ref), review_mode="independent-readings/1")
    return prefix, common, own, mapping, packet, original, chosen, correction, ref, raw


@pytest.mark.parametrize("mode", ["fixed", "own"])
def test_ordinary_ids_survive_duplicate_names_reordering_author_revision_and_frozen_export(
    own_dataset, tmp_path, monkeypatch, mode
):
    prefix, common, own, mapping, data, original, chosen, correction, ref, raw = admitted(
        legacy(), own_dataset, tmp_path, monkeypatch
    )
    by_id = {c["card_id"]: c for c in data["dossier"]["knowledge_cards"]}
    references = ModelReferences.create(EditorialInput.model_validate(data))
    chosen_wire, correction_wire = references.translate(dict(material_refs=[chosen, correction]))[
        "material_refs"
    ]
    provider = CardProvider(chosen_wire, correction_wire)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    if mode == "fixed":
        fixed = upgrade_fixed(
            json.loads((ROOT / "evals/editorial-inputs/v1/snow-pump/fixed-plan.json").read_text()), original
        )
        fixed["plan"]["plan"]["opening"]["material_refs"] = [chosen]
        for act in fixed["plan"]["plan"]["acts"]:
            act["material_refs"] = [chosen]
        fixed = rebind_fixed(fixed, original, data)
        fixed_path, fixed_ref = tmp_path / "fixed.json", tmp_path / "fixed-ref.json"
        fixed_path.write_text(json.dumps(fixed, ensure_ascii=False))
        assert (
            invoke(
                prefix,
                "fixed-editorial-import",
                input=fixed_path,
                source_map=mapping,
                shared_ref="@" + str(ref),
                out=fixed_ref,
            )
            == 0
        )
        args = dict(common, fixed_plan_input_ref="@" + str(fixed_ref))
    else:
        args = own
    assert invoke(prefix, "run", **args) == 0
    views = {r["metadata"]["podgen_stage"]: payload(r) for r in provider.requests}
    if mode == "own":
        cards = views["plan"]["dossier"]["knowledge_cards"]
        assert {c["card_id"] for c in cards} == set(references.aliases)
        assert views["plan-review"]["candidate_plan"]["plan"]["acts"][0]["material_refs"] == [chosen_wire]
    assert [c["claim"] for c in views["draft"]["support"]] == [by_id[chosen]["claim"]]
    assert {c["claim"] for c in views["revise-1"]["support"]} == {
        by_id[chosen]["claim"],
        by_id[correction]["claim"],
    }
    assert all("card_id" not in c for stage in ("draft", "revise-1") for c in views[stage]["support"])
    findings = views["revise-1"]["revision_guidance"][1]["findings"]
    documents = {doc["key"]: doc["title"] for doc in data["documents"]}
    for finding, identity in zip(findings, (correction, chosen), strict=True):
        assert "support_labels" not in finding
        expected = {(documents[q["document"]], q["text"]) for q in by_id[identity]["excerpts"]}
        assert {(q["source"], q["text"]) for q in finding["evidence"]} == expected
    assert "repair_evidence" not in views["revise-1"]
    assert findings[0]["anchors"][0]["utterance_index"] != findings[1]["anchors"][0]["utterance_index"]
    assert all(not {"start", "end", "document_id"} & set(p) for p in views["fact-review"]["evidence"])
    out = tmp_path / "export"
    report = (out / "generated-plan.md").read_text() if mode == "own" else ""
    if report:
        assert "Два одинаковых имени [" + chosen_wire + "]" in report
        assert chosen not in report and correction not in report
    assert (out / "editorial-input-original.json").read_bytes() == raw
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    assert journal.manifest(args["run_id"])[CATALOG_KEY] == references.as_payload()
    if mode == "own":
        invocation = journal.invocation(args["run_id"], "plan")
        record = journal.output(invocation)
        assert record["output"]["plan"]["opening"]["material_refs"] == [chosen]
        assert record["wire_output"]["plan"]["opening"]["material_refs"] == [chosen_wire]
    record = journal.output(journal.invocation(args["run_id"], "fact-review"))
    assert record["output"]["findings"][0]["support_labels"] == [correction]
    assert record["wire_output"]["findings"][0]["support_labels"] == [correction_wire]
    saved = journal.repo.get_runtime(own_dataset[1], RuntimeRef.model_validate_json(ref.read_bytes()))
    assert saved["content"] == EditorialInput.model_validate(data).model_dump(mode="json")
    # Workspace edits cannot rename sources of an already frozen run/report.
    (tmp_path / "v2.json").write_text("{}")
    count = len(provider.requests)
    assert invoke(prefix, "resume", run_id=args["run_id"], export_dir=out) == 0
    assert len(provider.requests) == count
    if report:
        assert (out / "generated-plan.md").read_text() == report


@pytest.mark.parametrize("bad_plan,bad_fact", [(True, False), (False, True)])
def test_foreign_returned_id_stops_before_author_or_rewrite(
    own_dataset, tmp_path, monkeypatch, bad_plan, bad_fact
):
    prefix, _, own, _, data, _, chosen, correction, _, _ = admitted(
        legacy(), own_dataset, tmp_path, monkeypatch
    )
    wire = ModelReferences.create(EditorialInput.model_validate(data)).translate(
        dict(material_refs=[chosen, correction])
    )["material_refs"]
    provider = CardProvider(*wire, bad_plan=bad_plan, bad_fact=bad_fact)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    assert invoke(prefix, "run", **own) == 2
    stages = [r["metadata"]["podgen_stage"] for r in provider.requests]
    assert ("draft" not in stages) if bad_plan else ("revise-1" not in stages)


@pytest.mark.parametrize("kind", ["label", "title", "document", "foreign"])
def test_plan_editor_rejects_names_document_keys_and_foreign_card_ids(kind):
    from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput
    from tests.own_plan_provider import proposal

    data, _ = upgrade_shared(legacy())
    packet = EditorialInput.model_validate(data)
    identifiers = catalog(packet)
    plan = PlanningOutput.model_validate(
        proposal(
            dict(material_catalog=identifiers, speaker_labels=[s.label for s in packet.assignment.speakers]),
            "identity-test",
        )
    )
    invalid = {
        "label": packet.dossier.knowledge_cards[0].label,
        "title": packet.dossier.knowledge_cards[0].title,
        "document": packet.documents[0].key,
        "foreign": "Cforeign",
    }[kind]
    review = PlanReviewOutput.model_validate(
        dict(
            decision="revise",
            decision_reason="Причина",
            listener_answer="Ответ",
            strengths=[],
            findings=[
                dict(
                    targets=["/plan/main_question"],
                    priority="important",
                    observation="Замечание",
                    requested_change="Изменение",
                    preserve=[],
                    material_refs=[invalid],
                )
            ],
            research_requests=[],
            duration_note="Не измерена",
        )
    )
    with pytest.raises(ValueError, match="unknown_material_locator"):
        review.validate_against(plan, material_locators=identifiers)


def test_current_packet_migration_preserves_exact_legacy_bytes_and_reference_bindings():
    for case in ("gps-clock", "snow-pump"):
        folder = ROOT / "evals/editorial-inputs/current" / case
        source = (
            ROOT / "evals/editorial-inputs/v1/gps-clock/shared.json"
            if case == "gps-clock"
            else folder / "legacy-shared.json"
        )
        raw = source.read_bytes()
        import hashlib

        mapping = json.loads((folder / "card-id-migration.json").read_text())
        before = EditorialInput.model_validate_json(raw)
        packet = EditorialInput.model_validate_json((folder / "shared.json").read_bytes())
        assert mapping["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert mapping["source_digest"] == digest(before.model_dump(mode="json"))
        assert mapping["target_digest"] == digest(packet.model_dump(mode="json"))
        assert packet.contract == "editorial-input/2"
        by_old = {c.label: c for c in before.dossier.knowledge_cards}
        for card in packet.dossier.knowledge_cards:
            previous = by_old[card.label]
            assert card.card_id == mapping["card_ids"][previous.label]
            assert card.title == previous.title and card.claim == previous.claim
            assert card.context == previous.context and card.limitations == previous.limitations
            assert card.excerpts == previous.excerpts
        assert packet.documents == before.documents
