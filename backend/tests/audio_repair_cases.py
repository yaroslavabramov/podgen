"""Complete native repair graph with synthetic transport fixtures; no acoustic qualification claims."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from podgen import audio_adjudication_cli, audio_repair_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.adjudication.attempts import history
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfig
from podgen.runtime.evaluation.audio.adjudication.contracts import prefix as review_prefix
from podgen.runtime.evaluation.audio.exchange_history import attempts
from podgen.runtime.evaluation.audio.fidelity.processing import revisions
from podgen.runtime.evaluation.audio.repair.contracts import RepairConfig, prefix
from podgen.runtime.evaluation.audio.repair.export import export
from podgen.runtime.evaluation.audio.repair.prepare import prepare
from podgen.runtime.evaluation.audio.repair.processing import process
from podgen.runtime.evaluation.audio.repair.service import run
from tests.test_audio_repair import ConfirmedReviewer, Synthesis


def assert_native_repair(journal, project, parent_ref, part_refs, fidelity, folder, monkeypatch):
    prompt = Path(__file__).resolve().parents[2] / "evals/audio/listening/local-fidelity-review.prompt.md"
    review = ReviewConfig(
        review_id=uuid4(),
        comparison_run_id=journal.run_id,
        fidelity_audit_id=fidelity.audit_id,
        candidate_result_ref=journal.ref(revisions(journal, fidelity.audit_id)[-1]["key"] + ":result"),
        prompt_path=str(prompt),
        prompt_sha256=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        price=dict(
            usd_per_million_input_tokens="1",
            usd_per_million_output_tokens="2",
            source_url="https://example.org/fixture",
            checked_at="2026-11-12",
        ),
        reservation_per_request_usd="0.10",
        max_cost_usd="1",
    )
    common = [
        "--project",
        str(project),
        "--actor",
        str(journal.repo.actor_id),
        "--objects",
        str(journal.repo.objects.root),
    ]
    path = folder / "repair-review.json"
    path.write_text(review.model_dump_json())
    monkeypatch.setattr(audio_adjudication_cli, "RawAudioTransport", lambda **kwargs: ConfirmedReviewer())
    monkeypatch.setenv("JEMINI_API_KEY", "fixture-review")
    assert (
        audio_adjudication_cli.main(
            [*common, "run", "--config", str(path), "--export-dir", str(folder / "repair-review")]
        )
        == 0
    )
    window = journal.get(review_prefix(review.review_id) + ":manifest")["windows"][0]
    row = history(journal, window["key"])[-1]
    evidence_ref = journal.ref(row["processing"][-1]["key"] + ":result")
    case_id = next(iter(journal.get(window["key"])["case_aliases"].values()))
    cfg = RepairConfig(
        repair_id=uuid4(),
        comparison_run_id=journal.run_id,
        parent_subject=dict(kind="assembly", ref=parent_ref.model_dump(mode="json")),
        part_source=dict(kind="comparison_result", ref=part_refs[0].model_dump(mode="json")),
        review_id=review.review_id,
        review_result_ref=evidence_ref,
        candidate_id=case_id,
        reason="Confirmed fixture evidence; regenerate whole original fragment",
        reservation_per_request_usd="0.50",
        max_cost_usd="1",
    )
    wrong = cfg.model_copy(
        update=dict(repair_id=uuid4(), part_source=cfg.part_source.model_copy(update=dict(ref=part_refs[1])))
    )
    with pytest.raises(ContractError, match="different_fragment"):
        prepare(journal, wrong)
    original = journal.base.read(parent_ref)
    path = folder / "repair.json"
    path.write_text(cfg.model_dump_json())
    destination = folder / "repair-export"
    args = ["--config", str(path), "--export-dir", str(destination)]
    transport = Synthesis()
    monkeypatch.setattr(audio_repair_cli, "HttpSynthesisTransport", lambda **kwargs: transport)
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "fixture-tts")
    assert audio_repair_cli.main([*common, "prepare", *args]) == 0 and not transport.calls
    assert audio_repair_cli.main([*common, "run", *args]) == 0 and len(transport.calls) == 1
    monkeypatch.delenv("TEST_SYNTHESIS_KEY")
    assert audio_repair_cli.main([*common, "resume", *args]) == 0 and len(transport.calls) == 1
    root = prefix(cfg.repair_id)
    manifest = journal.get(root + ":manifest")
    assert transport.calls[0] == journal.bytes(root + ":manifest", manifest["wire_blob"])
    rows = attempts(journal, root)
    first = rows[-1]["processing"][-1]
    result_ref = journal.ref(first["key"] + ":result")
    current = json.loads((destination / "current.json").read_text())
    assert current["result_ref"] == result_ref.model_dump(mode="json") and current["outcome_path"]
    outcome = json.loads((destination / current["outcome_path"]).read_text())
    repaired = journal.base.read(RuntimeRef.model_validate(outcome["assembly_ref"]))
    assert repaired["metrics"]["duration_seconds"] == 11 and repaired["joins"][0]["at_master_frame"] == 288000
    assert repaired["frame_map"][1]["source_result_ref"] == part_refs[1].model_dump(mode="json")
    assert repaired["frame_map"][0]["source_kind"] == "repair_part"
    assert (
        repaired["source_script_hash"] == original["source_script_hash"]
        and journal.base.read(parent_ref) == original
    )
    assert repaired["frame_map"][1]["source_pcm_sha256"] == original["frame_map"][1]["source_pcm_sha256"]
    assert outcome["verification_configuration"]["subject"]["ref"] == outcome["assembly_ref"]
    verification = journal.base.read(RuntimeRef.model_validate(outcome["verification_manifest_ref"]))
    assert verification["audio_blob"]["sha256"] == repaired["master_pcm_blob"]["sha256"]
    assert not journal.get("fidelity:" + outcome["verification_configuration"]["audit_id"] + ":submit")
    # New decoded revision without its own outcome must not inherit the older master.
    old_file = destination / current["replacement_master_path"] / "master.wav"
    old_hash = hashlib.sha256(old_file.read_bytes()).hexdigest()
    old_mtime = old_file.stat().st_mtime_ns
    assert (
        process(journal, cfg, manifest, rows[-1]["key"], "New local processing revision") == "part_generated"
    )
    second = attempts(journal, root)[-1]["processing"][-1]
    second_ref = journal.ref(second["key"] + ":result")
    status = export(journal, cfg, destination)
    assert status["current"]["result_ref"] == second_ref.model_dump(mode="json")
    assert (
        status["current"]["outcome_path"] is None
        and status["status"] == "reassembly_or_verification_required"
    )
    assert (
        old_file.stat().st_mtime_ns == old_mtime
        and hashlib.sha256(old_file.read_bytes()).hexdigest() == old_hash
    )
    assert run(journal, cfg, transport) == "verification_required" and len(transport.calls) == 1
    export(journal, cfg, destination)
    next_pointer = json.loads((destination / "current.json").read_text())
    assert next_pointer["replacement_master_path"] != current["replacement_master_path"]
    assert old_file.stat().st_mtime_ns == old_mtime
