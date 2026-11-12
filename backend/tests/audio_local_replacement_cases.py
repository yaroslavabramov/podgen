"""Native PostgreSQL proposal/approval/short-TTS/splice chain; provider is a mechanical double."""

import json
from uuid import uuid4

from podgen import audio_local_replacement_cli as cli
from podgen.audio.assembly.pcm import read
from podgen.domain.contracts.common import BlobRef
from podgen.runtime.evaluation.audio.local_replacement.contracts import ProposalConfig, prefix
from tests.audio_cases import response, signal


def assert_native_local_replacement(journal, project, parent_ref, fidelity, folder, monkeypatch):
    config = ProposalConfig(
        proposal_id=uuid4(),
        comparison_run_id=journal.run_id,
        parent_subject=dict(kind="assembly", ref=parent_ref.model_dump(mode="json")),
        approximate_seconds=0.1,
        recognition_audit_id=fidelity.audit_id,
        reason="Human requests the first sentence again.",
    )
    path, output = folder / "local-replacement.json", folder / "local-proposal"
    path.write_text(config.model_dump_json())
    common = [
        "--project",
        str(project),
        "--actor",
        str(journal.repo.actor_id),
        "--run-id",
        str(journal.run_id),
        "--objects",
        str(journal.repo.objects.root),
    ]
    assert cli.main([*common, "prepare", "--config", str(path), "--export-dir", str(output)]) == 0
    proposal = journal.get(prefix(config.proposal_id) + ":proposal")
    assert proposal["payload"]["error_source"] == "human"
    calls = []

    class Synthesis:
        def submit(self, request, credential):
            calls.append(request)
            assert all(
                row["text"] in proposal["payload"]["text"]
                for row in proposal["payload"]["synthesis_input"]["utterances"]
            )
            return response(request.provider, signal(seconds=2))

    monkeypatch.setattr(cli, "HttpSynthesisTransport", lambda **kwargs: Synthesis())
    monkeypatch.setenv(proposal["payload"]["profile"]["credential_env"], "fake-short-tts")
    args = ["--proposal-id", str(config.proposal_id), "--export-dir", str(output)]
    assert cli.main([*common, "synthesize", *args]) == 2 and not calls
    assert (
        cli.main(
            [
                *common,
                "approve",
                *args,
                "--proposal-hash",
                proposal["proposal_hash"],
                "--reason",
                "Exact mechanical fixture approval",
                "--reservation-usd",
                "1",
                "--max-cost-usd",
                "1",
            ]
        )
        == 0
    )
    assert cli.main([*common, "synthesize", *args]) == 0 and len(calls) == 1
    assert cli.main([*common, "resume", *args]) == 0 and len(calls) == 1
    master_dir = folder / "local-master"
    assert (
        cli.main(
            [*common, "apply", "--proposal-id", str(config.proposal_id), "--export-dir", str(master_dir)]
        )
        == 0
    )
    result = json.loads((master_dir / "result.json").read_text())
    parent = journal.base.read(parent_ref)
    parent_audio = journal.base.read_blob(parent_ref, BlobRef.model_validate(parent["master_pcm_blob"]))
    master = (master_dir / "master.wav").read_bytes()
    params, _, pcm = read(master)
    _, _, old = read(parent_audio)
    edit = result["edits"][0]
    stride = params[1] * params[0]
    assert pcm[: edit["new_start_frame"] * stride] == old[: edit["start_frame"] * stride]
    assert pcm[edit["new_end_frame"] * stride :] == old[edit["end_frame"] * stride :]
    assert len(result["joins"]) == 2
    ref = journal.ref("local-master:" + result["version_id"] + ":result")
    following = config.model_copy(
        update=dict(
            proposal_id=uuid4(),
            recognition_audit_id=None,
            parent_subject=config.parent_subject.model_copy(update=dict(kind="local_master", ref=ref)),
            approximate_seconds=(
                edit["new_start_frame"] + 0.5 * (edit["new_end_frame"] - edit["new_start_frame"])
            )
            / params[2],
        )
    )
    next_path = folder / "local-next.json"
    next_path.write_text(following.model_dump_json())
    assert (
        cli.main([*common, "prepare", "--config", str(next_path), "--export-dir", str(folder / "local-next")])
        == 0
    )
    next_proposal = journal.get(prefix(following.proposal_id) + ":proposal")
    assert next_proposal["payload"]["selected_units"][0]["timing_basis"] == "replacement_audio_group"
    assert len(calls) == 1
