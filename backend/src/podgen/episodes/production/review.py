"""After assembly: the user's review page, repairs of confirmed findings and acceptance of the master."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.journal import AudioJournal

from .audio import audit_config
from .contracts import ProductionConfig

ACCEPTANCE = "episode-audio-acceptance/1"


def context(repo, project, row):
    """The configuration, render journal, final verification and the master the user hears now."""
    config = ProductionConfig.model_validate(row["configuration"])
    verification = row["progress"].get("audio_verification")
    if (
        row["status"] not in ("assembled", "accepted")
        or not verification
        or verification.get("contract") != "episode-audio-verification/3"
    ):
        raise ContractError("episode_review_requires_verified_master")
    audio = AudioJournal(repo, project, uuid5(config.run_id, "audio-render/2"))
    return config, audio, verification, row["progress"].get("audio_master") or verification["master"]


def final_audit(audio, config, verification):
    name = f"audio-verification/1:{verification['repair_rounds']}"
    return audit_config(audio, config, config.settings.audio_verification, verification["master"], name)


def export_review(repo, project, row, folder):
    """Recognition findings of the verified master with clips, as files and a readable page."""
    from podgen.runtime.evaluation.audio.fidelity.export import export as export_fidelity

    from .review_page import write_page

    config, audio, verification, master = context(repo, project, row)
    audit = final_audit(audio, config, verification)
    folder = Path(folder)
    export_fidelity(audio, audit, folder / "recognition")
    status = json.loads((folder / "recognition/status.json").read_text())
    report = json.loads((folder / "recognition" / status["current"]["current_result"]["path"]).read_text())[
        "report"
    ]
    write_page(
        folder,
        report,
        folder / "recognition",
        topic=config.topic,
        repairs=row["progress"].get("audio_repairs"),
    )
    return dict(page=str(folder / "index.html"), master=master, audit_id=str(audit.audit_id))


def repair(repo, project, journal, candidate_ids, synthesis, recognition, reason):
    """Re-synthesize the sentences of confirmed findings in context and splice them in one new master."""
    from podgen.runtime.evaluation.audio.local_replacement import localization
    from podgen.runtime.evaluation.audio.local_replacement.apply import apply
    from podgen.runtime.evaluation.audio.local_replacement.approval import approve
    from podgen.runtime.evaluation.audio.local_replacement.contracts import ProposalConfigV3, prefix
    from podgen.runtime.evaluation.audio.local_replacement.localization import binding
    from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
    from podgen.runtime.evaluation.audio.local_replacement.service import synthesize

    row = journal.read()
    config, audio, verification, master = context(repo, project, row)
    if row["status"] == "accepted":
        raise ContractError("episode_review_already_accepted")
    if master != verification["master"]:
        raise ContractError("episode_repair_requires_findings_of_the_current_master")
    if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids) or not reason.strip():
        raise ContractError("episode_repair_requires_unique_findings_and_reason")
    settings, profile = config.settings.audio_verification, config.settings.audio_profile
    audit = verification["final_audit_id"]
    proposals = []
    for candidate in candidate_ids:
        proposal_id = uuid5(config.run_id, f"episode-repair:{audit}:{candidate}")
        proposal_config = ProposalConfigV3(
            proposal_id=proposal_id,
            comparison_run_id=audio.run_id,
            parent_subject=master,
            recognition_audit_id=UUID(audit),
            candidate_id=candidate,
            reason=reason,
            localization=dict(
                recognition=settings.recognition, credential_env=settings.credential_env, price=settings.price
            ),
        )
        with audio.lock():
            proposal = prepare(audio, proposal_config)
        if audio.get(prefix(proposal_id) + ":approval") is None:
            # Running this command is the user's confirmation of the finding.
            approve(
                audio,
                proposal_id,
                dict(
                    proposal_hash=proposal["proposal_hash"],
                    reason=reason,
                    reservation_per_request_usd=profile.reservation_usd,
                    max_cost_usd=profile.reservation_usd,
                    max_requests=1,
                    localization_budget=dict(
                        reservation_per_request_usd=settings.reservation_usd,
                        max_cost_usd=settings.reservation_usd,
                        max_requests=1,
                    ),
                ),
            )
        status = synthesize(audio, proposal_id, synthesis)
        if status != "synthesized":
            raise ContractError("episode_repair_" + status)
        status = localization.run(audio, proposal_id, recognition)
        if status != "extraction_ready":
            raise ContractError("episode_repair_" + status)
        check = audio.get(binding(audio, proposal_id)[3] + ":result").get("target_check")
        proposals.append(
            dict(
                proposal_id=str(proposal_id),
                candidate_id=str(candidate),
                text=proposal["payload"]["text"],
                target_check=check,
            )
        )
    ref = apply(audio, [UUID(item["proposal_id"]) for item in proposals])
    progress = dict(
        row["progress"],
        audio_master=dict(kind="local_master", ref=ref),
        audio_repairs=dict(contract="episode-audio-repairs/1", audit_id=audit, proposals=proposals),
    )
    return journal.review(progress, "assembled")


def accept(repo, project, journal, reason):
    """The user accepts exactly the master they listened to."""
    from podgen.runtime.evaluation.audio.subject_contracts import AudioSubject
    from podgen.runtime.evaluation.audio.subjects import audio_subject

    row = journal.read()
    _, audio, _, master = context(repo, project, row)
    if not reason.strip():
        raise ContractError("episode_acceptance_requires_reason")
    subject = audio_subject(audio, AudioSubject.model_validate(master))
    acceptance = dict(
        contract=ACCEPTANCE,
        actor_id=str(repo.actor_id),
        master=master,
        audio_sha256=subject["audio_blob"]["sha256"],
        reason=reason,
        accepted_at=datetime.now(timezone.utc).isoformat(),
    )
    previous = row["progress"].get("audio_acceptance")
    if previous:
        if (previous["master"], previous["audio_sha256"]) != (master, acceptance["audio_sha256"]):
            raise ContractError("episode_accepted_master_changed")
        return row
    return journal.review(dict(row["progress"], audio_acceptance=acceptance), "accepted")


def export_package(repo, project, row, folder):
    """The accepted master, its script and the acceptance record."""
    from podgen.domain.contracts.common import BlobRef
    from podgen.runtime.evaluation.audio.subject_contracts import AudioSubject
    from podgen.runtime.evaluation.audio.subjects import audio_subject
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.reading_validation import read_descriptor

    acceptance = row["progress"].get("audio_acceptance")
    if row["status"] != "accepted" or not acceptance:
        raise ContractError("episode_package_requires_acceptance")
    _, audio, _, master = context(repo, project, row)
    binding = AudioSubject.model_validate(master)
    subject = audio_subject(audio, binding)
    wav = audio.base.read_blob(binding.ref, BlobRef.model_validate(subject["audio_blob"]))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "master.wav").write_bytes(wav)
    script = read_descriptor(
        EvaluationJournal(repo, project), UUID(row["progress"]["writing_run_id"])
    ).outcome["output"]
    (folder / "script.txt").write_text(
        "\n\n".join(f"{u['speaker']}: {u['text']}" for u in script["utterances"]) + "\n", encoding="utf-8"
    )
    (folder / "acceptance.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n")
    return dict(folder=str(folder), audio_sha256=acceptance["audio_sha256"])
