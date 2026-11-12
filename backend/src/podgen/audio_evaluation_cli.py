"""Run with python -m podgen.audio_evaluation_cli; preparation and export never synthesize."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from podgen.audio.synthesis.transport import HttpSynthesisTransport
from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.audio.comparison_directory import bind_comparison
from podgen.runtime.evaluation.audio.contracts import load_comparison_config
from podgen.runtime.evaluation.audio.export import export
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.listening.batch import ListeningConfig, build
from podgen.runtime.evaluation.audio.listening.export import export_batch
from podgen.runtime.evaluation.audio.prepare import prepare
from podgen.runtime.evaluation.audio.reprocess import reprocess
from podgen.runtime.evaluation.audio.service import run


def parser():
    p = argparse.ArgumentParser(description="Auditable voice comparison from approved evaluation outcomes")
    p.add_argument("--project", type=UUID, required=True)
    p.add_argument("--actor", type=UUID, required=True)
    p.add_argument("--objects", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    commands = p.add_subparsers(dest="operation", required=True)
    for op in ("prepare", "run", "resume", "retake", "reprocess", "export", "listen", "listen-export"):
        child = commands.add_parser(op)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--export-dir", type=Path, required=True)
        if op in ("listen", "listen-export"):
            child.add_argument("--batch-id", type=UUID, required=True)
        if op == "listen":
            child.add_argument("--preferred-lufs", type=float, default=-20)
            child.add_argument("--true-peak-dbtp", type=float, default=-1)
            child.add_argument("--headroom-db", type=float, default=0.3)
            child.add_argument("--blind-seed", type=int, default=42)
            child.add_argument("--repair-observed-fish-header", action="store_true")
        if op in ("retake", "reprocess"):
            child.add_argument("--sample-id", required=True)
            child.add_argument("--reason", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    config = load_comparison_config(args.config.read_text())
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    journal = AudioJournal(
        Repository(db, FileObjectStore(args.objects), args.actor, purpose="evaluation"),
        args.project,
        config.run_id,
    )
    try:
        if args.operation in ("listen", "listen-export"):
            if args.operation == "listen":
                build(
                    journal,
                    config,
                    ListeningConfig(
                        batch_id=args.batch_id,
                        preferred_lufs=args.preferred_lufs,
                        true_peak_dbtp=args.true_peak_dbtp,
                        headroom_db=args.headroom_db,
                        blind_seed=args.blind_seed,
                        repair_observed_fish_header=args.repair_observed_fish_header,
                    ),
                )
            result = export_batch(journal, args.batch_id, args.export_dir)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        if args.operation == "prepare":
            with journal.lock():
                prepare(journal, config)
        elif args.operation == "reprocess":
            reprocess(journal, config, args.sample_id, args.reason)
        elif args.operation in ("run", "resume", "retake"):
            with journal.lock():
                manifest = prepare(journal, config)
                bind_comparison(journal, args.export_dir, manifest)
            run(
                journal,
                config,
                HttpSynthesisTransport(timeout_seconds=config.http_timeout_seconds),
                progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True),
                retake_cell=getattr(args, "sample_id", None),
                reason=getattr(args, "reason", None),
            )
        result = export(journal, args.export_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if args.operation == "prepare" or result["requested_matrix_complete"] else 2
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
