"""Human-directed, versioned local audio replacement; paid calls require exact proposal approval."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from podgen.audio.http import RawAudioTransport
from podgen.audio.synthesis.transport import HttpSynthesisTransport
from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.audio.export_directory import write_json
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.local_replacement import localization
from podgen.runtime.evaluation.audio.local_replacement.apply import apply
from podgen.runtime.evaluation.audio.local_replacement.approval import approve, authorized, verify_proposal
from podgen.runtime.evaluation.audio.local_replacement.contracts import ProposalConfigV2, contextual, prefix
from podgen.runtime.evaluation.audio.local_replacement.export import export_master, export_proposal
from podgen.runtime.evaluation.audio.local_replacement.prepare import prepare
from podgen.runtime.evaluation.audio.local_replacement.service import reprocess, synthesize
from podgen.runtime.evaluation.audio.subject_contracts import AudioSubject


def parser():
    p = argparse.ArgumentParser(
        description="Prepare, approve, synthesize and splice a short human-selected audio range"
    )
    for name in ("project", "actor", "run-id"):
        p.add_argument("--" + name, type=UUID, required=True)
    p.add_argument("--objects", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    commands = p.add_subparsers(dest="operation", required=True)
    for op in (
        "prepare",
        "revise",
        "approve",
        "synthesize",
        "resume",
        "retry",
        "reprocess",
        "apply",
        "export",
        "export-master",
    ):
        child = commands.add_parser(op)
        child.add_argument("--export-dir", type=Path, required=True)
        if op == "prepare":
            child.add_argument("--config", type=Path, required=True)
        elif op == "export-master":
            child.add_argument("--master-ref", required=True)
        else:
            child.add_argument(
                "--proposal-id", type=UUID, required=True, action="append" if op == "apply" else "store"
            )
        if op == "revise":
            child.add_argument("--localization-config", type=Path)
            child.add_argument("--new-proposal-id", type=UUID)
            child.add_argument("--shift-sentences", type=int, default=0)
            child.add_argument("--sentence-count", type=int)
            child.add_argument("--approximate-seconds", type=float)
        if op == "approve":
            child.add_argument("--proposal-hash", required=True)
            child.add_argument("--reservation-usd", required=True)
            child.add_argument("--max-cost-usd", required=True)
            child.add_argument("--max-requests", type=int, default=1)
            child.add_argument("--asr-reservation-usd")
            child.add_argument("--asr-max-cost-usd")
            child.add_argument("--asr-max-requests", type=int, default=1)
        if op in ("retry", "reprocess"):
            child.add_argument("--stage", choices=("tts", "asr"), default="tts")
        if op in ("approve", "retry", "reprocess"):
            child.add_argument("--reason", required=True)
        if op == "apply":
            child.add_argument("--parent-subject")
    return p


def execute(journal, args):
    op = args.operation
    if op == "export-master":
        return export_master(journal, json.loads(args.master_ref), args.export_dir)
    if op == "apply":
        parent = AudioSubject.model_validate_json(args.parent_subject) if args.parent_subject else None
        return export_master(
            journal, apply(journal, args.proposal_id, parent_subject=parent), args.export_dir
        )
    if op in ("prepare", "revise"):
        if op == "prepare":
            config = ProposalConfigV2.model_validate_json(args.config.read_bytes())
        else:
            old = verify_proposal(journal, args.proposal_id)["payload"]["configuration"]
            changed = dict(
                old,
                proposal_id=str(args.new_proposal_id or uuid4()),
                parent_proposal_ref=journal.ref(prefix(args.proposal_id) + ":proposal").model_dump(
                    mode="json"
                ),
            )
            changed["sentence_offset"] = old["sentence_offset"] + args.shift_sentences
            if args.sentence_count is not None:
                changed["sentence_count"] = args.sentence_count
            if args.approximate_seconds is not None:
                changed.update(
                    approximate_seconds=args.approximate_seconds, sentence_offset=args.shift_sentences
                )
            changed["contract"] = "audio-local-replacement-proposal/2"
            if args.localization_config:
                changed["localization"] = json.loads(args.localization_config.read_bytes())
            if "localization" not in changed:
                raise ContractError("local_replacement_revision_requires_contextual_config_with_asr_profile")
            config = ProposalConfigV2.model_validate(changed)
        with journal.lock():
            prepare(journal, config)
        return dict(
            export_proposal(journal, config.proposal_id, args.export_dir), proposal_id=str(config.proposal_id)
        )
    operation_result = None
    # Directory ownership is checked before approval or a potentially paid request.
    export_proposal(journal, args.proposal_id, args.export_dir)
    if op == "approve":
        proposal = verify_proposal(journal, args.proposal_id)
        value = dict(
            proposal_hash=args.proposal_hash,
            reason=args.reason,
            reservation_per_request_usd=args.reservation_usd,
            max_cost_usd=args.max_cost_usd,
            max_requests=args.max_requests,
        )
        if contextual(proposal["payload"]):
            value["localization_budget"] = dict(
                reservation_per_request_usd=args.asr_reservation_usd,
                max_cost_usd=args.asr_max_cost_usd,
                max_requests=args.asr_max_requests,
            )
        approve(journal, args.proposal_id, value)
    elif op in ("synthesize", "resume", "retry", "reprocess"):
        proposal, approval = authorized(journal, args.proposal_id)
        is_contextual = contextual(proposal["payload"])
        stage = getattr(args, "stage", "tts")
        if stage == "asr" and not is_contextual:
            raise ContractError("local_replacement_localization_requires_contextual_proposal")
        if op == "reprocess" and stage == "tts":
            operation_result = reprocess(journal, args.proposal_id, args.reason)
        elif stage == "tts":
            operation_result = synthesize(
                journal,
                args.proposal_id,
                HttpSynthesisTransport(timeout_seconds=approval.request_timeout_seconds),
                retry_reason=args.reason if op == "retry" else None,
            )
        else:
            operation_result = "synthesized"
        if is_contextual and operation_result == "synthesized" and not (op == "reprocess" and stage == "tts"):
            operation_result = localization.run(
                journal,
                args.proposal_id,
                RawAudioTransport(timeout_seconds=approval.localization_budget.request_timeout_seconds),
                retry_reason=args.reason if op == "retry" and stage == "asr" else None,
                reprocess_reason=args.reason if op == "reprocess" and stage == "asr" else None,
            )
    status = export_proposal(journal, args.proposal_id, args.export_dir)
    if operation_result is not None:
        status["operation_result"] = operation_result
        write_json(args.export_dir / "status.json", status)
    return status


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    journal = AudioJournal(
        Repository(db, FileObjectStore(args.objects), args.actor, purpose="evaluation"),
        args.project,
        args.run_id,
    )
    try:
        result = execute(journal, args)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result.get("operation_result") in (None, "synthesized", "extraction_ready") else 2
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    except (ValueError, OSError):
        print(json.dumps(dict(ok=False, code="local_replacement_invalid_local_input")), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
