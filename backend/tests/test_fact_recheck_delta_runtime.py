"""Ordinary local-provider cycle: delta wire, full reports, writer, preview and replay."""

import json
from copy import deepcopy

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.workflow import profile_for
from podgen.runtime.models.contracts import WireReply
from podgen.writing.reading_audit.consistency import DELTA_VERSION
from tests.reading_audit_provider import AuditProvider
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import setup_review
from tests.test_own_plan import own_dataset as own_dataset


class DeltaProvider(AuditProvider):
    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        stage = body["metadata"]["podgen_stage"]
        if not stage.startswith("fact-review"):
            return reply
        envelope = json.loads(reply.body)
        message = next((row for row in envelope["output"] if row["type"] == "message"), None)
        if message is None:
            return reply
        value = json.loads(message["content"][0]["text"])
        payload = request_payload(
            dict(body, input=[item for item in body["input"] if item.get("role") == "user"])
        )
        if stage == "fact-review":
            stable = deepcopy(value["audit"]["claim_checks"][0])
            stable.update(
                decision_id="D2",
                anchors=[
                    dict(
                        utterance_index=len(payload["script"]["utterances"]) - 1,
                        quote=payload["script"]["utterances"][-1]["text"],
                    )
                ],
                contextual_meaning="An unchanged final contextual claim.",
                verdict="supported",
                rationale="The final claim is supported at the stated scope by the source read E1 and packet excerpt B1.",
                memory_summary="E1 and B1 support the final claim at this scope.",
            )
            value["audit"]["claim_checks"].append(stable)
            value["audit"]["coverage"][-1]["decision_ids"].append("D2")
            next(row for row in value["audit"]["areas"] if row["aspect"] == "factual_accuracy")[
                "decision_ids"
            ].append("D2")
        else:
            assert "previous_script_in_order" not in payload["audit_context"]
            assert payload["audit_context"]["script_changes"]
            assert "retained_confirmation" in body["text"]["format"]["schema"]["properties"]
            assert "Confirm the rest once" in body["instructions"]
            assert "Reconstruct the previous script" not in body["instructions"]
            value["retained_confirmation"] = dict(
                confirmed=True,
                grounds="The unchanged final claim still applies in the full current context with its prior inspected evidence.",
            )
            value["previous_dispositions"] = [
                row for row in value["previous_dispositions"] if row["previous_decision_id"] != "D2"
            ]
            previous = next(
                row for row in payload["audit_context"]["previous_decisions"] if row["decision_id"] == "D2"
            )
            assert previous["anchor_version"] == "current" and not previous["requires_update"]
            assert previous["memory_summary"] == "H1 and B1 support the final claim at this scope."
            value["audit"]["coverage"][-1]["decision_ids"].append("D2")
            next(row for row in value["audit"]["areas"] if row["aspect"] == "factual_accuracy")[
                "decision_ids"
            ].append("D2")
        message["content"][0]["text"] = json.dumps(value, ensure_ascii=False)
        return WireReply(json.dumps(envelope, ensure_ascii=False).encode())


def test_complete_delta_cycle_persists_full_audit_and_replays_without_provider(
    own_dataset, tmp_path, monkeypatch
):
    repo, project, *_ = own_dataset
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    for role, name in [("writer", "claude-opus-5-5"), ("reviewer", "gpt-6.1-terra")]:
        ref = create_profile(
            repo,
            project,
            model=name,
            provider="anthropic" if role == "writer" else "openai",
            output_limit=24576,
            context_limit=200000,
            reasoning_effort="medium",
        ).ref
        options[role + "_profile"] = ref.model_dump_json()
    options["behavior_profile"] = "production/1"
    provider = DeltaProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **kw: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *a, **kw: provider
    )
    assert invoke(prefix, "run", **options) == 0
    journal = EvaluationJournal(repo, project)
    run_id = options["run_id"]
    manifest = journal.manifest(run_id)
    assert manifest["reading_audit_consistency_contract"] == DELTA_VERSION
    first = journal.output(journal.invocation(run_id, "fact-review"))
    stable = first["output"]["audit"]["claim_checks"][1]
    generation = generation_repo(repo)
    command = WritingRunCommand.model_validate(manifest["command"])
    materials = load_editorial_materials(generation, project, command.author_ref, case_id=command.case_id)
    for stage in ("fact-review-1", "fact-review-2"):
        row = journal.invocation(run_id, stage)
        saved = journal.output(row)
        assert len(saved["wire_output"]["audit"]["claim_checks"]) == 1
        assert len(saved["output"]["audit"]["claim_checks"]) == 2
        kept = next(item for item in saved["output"]["audit"]["claim_checks"] if item["decision_id"] == "D2")
        assert kept["contextual_meaning"] == stable["contextual_meaning"]
        assert kept["rationale"] == stable["rationale"].replace("E1", "H1")
        assert kept["memory_summary"] == stable["memory_summary"].replace("E1", "H1")
        assert saved["reading_audit"]["recheck_delta"]["retained_decision_ids"] == ["D2"]
        assert saved["reading_audit"]["resolved_evidence"]["previous:H1"]["original_id"] == "E1"
        script = journal.read(RuntimeRef.model_validate(saved["script_ref"]))["output"]
        snapshot = load_preview_snapshot(journal, generation, run_id, stage, materials)
        profile = load_profile(generation, project, profile_for(command, stage), stage)
        assert preview_request(
            run_id, stage, profile, materials, snapshot=snapshot, draft=script
        ) == journal.request(row)
    calls = len(provider.requests)
    assert (
        writing_cli.main(
            [*prefix, "resume", "--run-id", str(run_id), "--export-dir", str(tmp_path / "export")]
        )
        == 0
    )
    assert len(provider.requests) == calls


def test_local_provider_stable_claim_does_not_inherit_required_defect():
    from podgen.writing.projections import indexed_draft
    from podgen.writing.reading_audit.input import initial_context
    from podgen.writing.reading_audit.registry import model
    from podgen.writing.reading_audit.validation import validate
    from tests.reading_audit_cases import SCRIPT

    context = initial_context(fact=True)
    body = dict(
        model="local",
        metadata=dict(podgen_stage="fact-review", podgen_source_turn="1"),
        input=[
            dict(
                role="user",
                content=[
                    dict(
                        type="input_text",
                        text=json.dumps(dict(script=indexed_draft(SCRIPT), audit_context=context)),
                    )
                ],
            )
        ],
    )
    reply = DeltaProvider().submit(body, client_request_id="offline")
    value = json.loads(json.loads(reply.body)["output"][0]["content"][0]["text"])
    parsed = model("fact-review").model_validate(value)
    validate(parsed.model_dump(mode="json"), SCRIPT, context, fact=True, consistency_contract=DELTA_VERSION)
    assert value["audit"]["claim_checks"][1]["verdict"] == "supported"
