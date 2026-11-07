"""Offline ordinary CLI recovery keeps the frozen V5 catalogue and original replies."""

import json
from copy import deepcopy

import pytest

from podgen import writing_cli
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.contracts import WireReply
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import CATALOG_KEY, ModelReferences
from tests.credit_provider import CreditProvider
from tests.historical_revisions import historical_revision_manifest
from tests.own_plan_provider import proposal
from tests.test_card_ids import admitted, legacy
from tests.test_editorial_independence import invoke, payload
from tests.test_independent_readings import Paused
from tests.test_listener_question_flow import QuestionFlowProvider
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_plan_author_contract import CurrentPlanProvider


class RecoveryProvider(CurrentPlanProvider):
    def __init__(self, references, packet, mode):
        super().__init__(
            ("revise", "keep"),
            invalid="fact-review" if mode == "anchor" else None,
            pause="plan-revise" if mode == "pending" else None,
        )
        initial = proposal(
            dict(
                material_catalog=references.aliases,
                speaker_labels=[s.label for s in packet.assignment.speakers],
            ),
            "wire",
        )
        initial["plan"]["acts"][0]["prerequisite_concepts"] = ["A prerequisite remains complete."]
        script = dict(
            utterances=[
                dict(speaker=s.label, text=f"Exact words {index}.")
                for index, s in enumerate(packet.assignment.speakers)
            ]
        )
        self.upstream = QuestionFlowProvider(initial, script, "The accepted question remains whole?")
        self.break_target = mode == "target"
        self.responses = {}

    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        stage = body["metadata"]["podgen_stage"]
        # Include actual short material refs in the review and its repaired version.
        if stage.startswith("plan-review") and body["text"]["format"]["name"] != "plan_target_choices":
            response = json.loads(reply.body)
            content = response["output"][0]["content"][0]
            review = json.loads(content["text"])
            for finding in review["findings"]:
                finding["material_refs"] = ["M1"]
            content["text"] = json.dumps(review, ensure_ascii=False)
            reply = WireReply(json.dumps(response, ensure_ascii=False).encode())
        self.responses[stage] = reply.body
        return reply


@pytest.mark.parametrize("mode", ["pending", "target", "anchor", "credit"])
def test_exact_v5_wire_survives_every_recovery_boundary(own_dataset, tmp_path, monkeypatch, mode):
    prefix, _, options, _, data, _, _, _, _, _ = admitted(legacy(), own_dataset, tmp_path, monkeypatch)
    packet = EditorialInput.model_validate(data)
    refs = ModelReferences.create(packet)
    inner = RecoveryProvider(refs, packet, mode)
    provider = CreditProvider(inner, stage="plan-revise") if mode == "credit" else inner
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    if mode == "anchor":
        options["review_anchor_policy"] = "bounded-selection-v1"
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    pending = None
    if mode == "pending":
        with pytest.raises(Paused):
            invoke(prefix, "run", **options)
        pending = journal.request(journal.invocation(options["run_id"], "plan-revise"))
        inner.pause = None
    else:
        assert invoke(prefix, "run", **options) == (2 if mode in ("target", "credit") else 0)
    saved_manifest = deepcopy(journal.manifest(options["run_id"]))
    assert saved_manifest[CATALOG_KEY] == refs.as_payload()
    resume = dict(run_id=options["run_id"], export_dir=tmp_path / "export")
    if mode == "target":
        resume["repair_plan_targets"] = journal.invocation(options["run_id"], "plan-review")["invocation_id"]
    if mode == "credit":
        failed = journal.invocation(options["run_id"], "plan-revise")
        pending = journal.request(failed)
        resume["retry_credit_refusal"] = failed["invocation_id"]
    assert invoke(prefix, "resume", **resume) == 0
    assert journal.manifest(options["run_id"]) == saved_manifest
    if pending:
        actual = next(
            body for body in reversed(inner.requests) if body["metadata"]["podgen_stage"] == "plan-revise"
        )
        assert {k: v for k, v in actual.items() if k != "metadata"} == {
            k: v for k, v in pending.items() if k != "metadata"
        }
    for stage in ("plan", "plan-revise"):
        invocation = journal.invocation(options["run_id"], stage)
        assert journal.last_response(invocation) == inner.responses[stage]
        record = journal.output(invocation)
        assert record["output"] == refs.translate(record["wire_output"], inbound=True)
        native = record["output"]["plan"]["acts"][0]["material_refs"]
        assert native == [refs.card_ids[-1]]
    review = journal.output(journal.invocation(options["run_id"], "plan-review"))
    assert review["output"]["findings"][0]["material_refs"] == [refs.card_ids[0]]
    assert review["wire_output"]["findings"][0]["material_refs"] == ["M1"]
    views = {body["metadata"]["podgen_stage"]: payload(body) for body in inner.requests}
    for stage in ("plan-review", "plan-review-revised"):
        actual = next(
            payload(body)
            for body in inner.requests
            if body["metadata"]["podgen_stage"] == stage and "dossier" in payload(body)
        )
        assert actual["dossier"] == views["plan"]["dossier"]
    assert set(views["plan-revise"]["dossier"]) == {"knowledge_cards"}
    assert views["draft"]["story"]["main_question"] == "The accepted question remains whole?"
    assert views["draft"]["story"]["acts"][0]["prerequisite_concepts"] == ["A prerequisite remains complete."]
    count = len(inner.requests)
    assert invoke(prefix, "resume", **resume) == 0
    assert len(inner.requests) == count


def test_v4_pending_run_keeps_native_ids_and_exact_saved_request(own_dataset, tmp_path, monkeypatch):
    from podgen.runtime.evaluation.role_schema import response_schema
    from podgen.writing.projection_versions import V4
    from tests.test_card_ids import CardProvider

    prefix, _, options, _, _, _, chosen, correction, _, _ = admitted(
        legacy(), own_dataset, tmp_path, monkeypatch
    )
    original = EvaluationJournal.create_run

    def pinned(journal, command, manifest):
        previous = historical_revision_manifest(manifest)
        previous.pop(CATALOG_KEY)
        previous.pop("reader_review_schema_version", None)
        previous.pop("craft_reference", None)
        previous["role_projection_version"] = V4
        previous["response_schemas"] = {
            stage: response_schema(stage, command.input_contract, projection_version=V4)
            for stage in previous["response_schemas"]
        }
        return original(journal, command, previous)

    monkeypatch.setattr(EvaluationJournal, "create_run", pinned)
    provider = CardProvider(chosen, correction)
    provider.pause = "fact-review"
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    with pytest.raises(Paused):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(own_dataset[0], own_dataset[1])
    pending = journal.request(journal.invocation(options["run_id"], "fact-review"))
    manifest = journal.manifest(options["run_id"])
    assert CATALOG_KEY not in manifest
    assert "reader_review_schema_version" not in manifest
    assert "threads" in payload(provider.requests[0])["dossier"]
    provider.pause = None
    assert invoke(prefix, "resume", run_id=options["run_id"], export_dir=tmp_path / "export") == 0
    assert (
        next(body for body in provider.requests if body["metadata"]["podgen_stage"] == "fact-review")
        == pending
    )
    assert journal.manifest(options["run_id"]) == manifest
    report = (tmp_path / "export/generated-plan.md").read_text()
    assert "Два одинаковых имени [" + chosen + "]" in report
