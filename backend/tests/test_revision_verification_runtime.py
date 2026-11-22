"""Run the published profiles through two edits, recorded previews and a restart without a provider."""

import json

import pytest

from podgen import writing_cli
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation import recheck_source
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.models.contracts import WireReply
from podgen.writing.revision import verification
from tests.source_excerpt_cases import pin_before_reading_audit
from tests.stage_pause import pause_before_count
from tests.test_editorial_independence import invoke, payload
from tests.test_independent_readings import ReadingProvider, setup_review
from tests.test_own_plan import own_dataset as own_dataset
from tests.test_revision_verification import reconstruct


class CheckingProvider(ReadingProvider):
    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        stage = body["metadata"]["podgen_stage"]
        if not stage.startswith(("reader-review", "fact-review")):
            return reply
        current = payload(body)
        envelope = json.loads(reply.body)
        content = envelope["output"][0]["content"][0]
        review = json.loads(content["text"])
        if stage.startswith("fact-review"):
            if "SourcedFactFinding" in body["text"]["format"]["schema"].get("$defs", {}):
                for finding in review["findings"]:
                    finding["source_read_ids"] = []
            content["text"] = json.dumps(review, ensure_ascii=False)
            return WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        if "revision_check" in current:
            review.update(
                reading_mode="informed",
                effect_on_episode="Проверенное завершение изменило ход рассказа.",
                assignment_results=[
                    dict(
                        assignment_id=assignment["id"],
                        outcome="solved_differently",
                        judgment="Перепись дала слушателю переход в текущем тексте.",
                        followup_finding=None,
                    )
                    for assignment in current["revision_check"]["assignments"]
                ],
            )
        if stage == "reader-review":
            scope = review["revision_scope"]
            old = current["script"]["utterances"]
            scope.update(
                scale="sequence",
                sequence_ranges=[
                    dict(
                        reason="Перестроить начало.",
                        anchors=[dict(utterance_index=i, quote=old[i]["text"]) for i in (0, 1)],
                    )
                ],
            )
        elif stage == "reader-review-1":
            review["revision_scope"]["scale"] = "whole_episode"
        content["text"] = json.dumps(review, ensure_ascii=False)
        return WireReply(json.dumps(envelope, ensure_ascii=False).encode())


@pytest.mark.parametrize("profile_name", ["editorial-candidate/1", "narrative-rebuild/1", "production/1"])
def test_pinned_second_editorial_reading_is_bound_to_each_real_revision_and_replays(
    own_dataset, tmp_path, monkeypatch, profile_name
):
    if profile_name == "production/1":
        pin_before_reading_audit(monkeypatch)
    prefix, options, initial = setup_review(own_dataset, tmp_path, monkeypatch)
    repo, project, *_ = own_dataset
    for role, model in (("writer", "claude-opus-5-5"), ("reviewer", "gpt-6.1-terra")):
        ref = create_profile(
            repo,
            project,
            model=model,
            provider="anthropic" if role == "writer" and profile_name == "production/1" else "openai",
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
        options[role + "_profile"] = ref.model_dump_json()
    options["behavior_profile"] = profile_name
    provider = CheckingProvider(("revise", "revise", "keep"))
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *args, **kwargs: provider
    )
    with pause_before_count(options["run_id"], "reader-review-1"):
        invoke(prefix, "run", **options)
    journal = EvaluationJournal(repo, project)
    run = options["run_id"]
    manifest = journal.manifest(run)
    candidate = profile_name in ("editorial-candidate/1", "production/1")
    pin = (
        verification.TASK_VERSION
        if profile_name == "production/1"
        else verification.VERSION
        if candidate
        else None
    )
    assert manifest.get(verification.KEY) == pin

    def request(stage):
        return journal.request(journal.invocation(run, stage))

    def output(stage):
        return journal.output(journal.invocation(run, stage))["output"]

    prior = initial

    def check_preview(stage, current):
        scriptfile = tmp_path / (stage + ".json")
        scriptfile.write_text(json.dumps(current, ensure_ascii=False))
        assert (
            invoke(
                prefix,
                "preview",
                run_id=run,
                case=options["case"],
                shared_ref=options["author_ref"],
                profile=options["reviewer_profile"],
                stage=stage,
                draft=scriptfile,
                out=tmp_path / "wire.json",
            )
            == 0
        )
        assert json.loads((tmp_path / "wire.json").read_text())["request"] == request(stage)

    assert "revision_check" not in payload(request("reader-review"))
    check_preview("reader-review-1", output("revise-1"))
    if candidate:
        with pytest.raises(Conflict, match="revision_verification_source_mismatch"):
            recheck_source.load(journal, run, "reader-review-1", version=pin, script=initial)

    assert invoke(prefix, "resume", run_id=run, export_dir=tmp_path / "export") == 0
    for number in (1, 2):
        stage = f"reader-review-{number}"
        current = output(f"revise-{number}")
        operation = payload(request(f"revise-{number}"))["revision_operation"]
        assert operation["mode"] == ("sequence" if number == 1 else "structural")
        view = payload(request(stage))
        assert "revision_check" not in payload(request(f"fact-review-{number}"))
        if candidate:
            assert reconstruct(view["revision_check"], current) == prior
            if profile_name == "production/1":
                writer_input = payload(request(f"revise-{number}"))
                assert view["revision_check"]["revision_operation"] == writer_input["revision_operation"]
                expected = [
                    {
                        key: value
                        for key, value in task.items()
                        if key in ("origin", "anchors", "problem", "action", "preserve")
                    }
                    for task in writer_input["revision_tasks"]
                ]
                assert [
                    {key: value for key, value in task.items() if key != "id"}
                    for task in view["revision_check"]["assignments"]
                ] == expected
                assert "revision_check" not in writer_input
            assert {row["assignment_id"] for row in output(stage)["assignment_results"]} == {
                row["id"] for row in view["revision_check"]["assignments"]
            }
            assert output(stage)["reading_mode"] == "informed"
        else:
            assert "revision_check" not in view and output(stage)["reading_mode"] == "script_only"
        check_preview(stage, current)
        prior = current
    assert read_outcome(journal, run)["output"] == prior
    if candidate:
        assert "Проверенное завершение" in (tmp_path / "export/review-result.md").read_text()
    before = list(provider.requests)
    assert invoke(prefix, "resume", run_id=run, export_dir=tmp_path / "export") == 0
    assert provider.requests == before
