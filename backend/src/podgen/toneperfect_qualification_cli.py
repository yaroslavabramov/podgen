"""Isolated eight-control qualification operator; never changes production settings."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from podgen.audio.http import RawAudioTransport
from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation.toneperfect.contracts import Trial
from podgen.runtime.evaluation.toneperfect.export import export
from podgen.runtime.evaluation.toneperfect.prepare import prepare
from podgen.runtime.evaluation.toneperfect.service import run
from podgen.runtime.evaluation.toneperfect.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="TonePerfect qualification only")
    parser.add_argument("--env-file", type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("prepare", "run", "export"):
        child = commands.add_parser(operation)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--output-dir", type=Path, required=True)
        if operation == "run":
            child.add_argument("--case-id")
            child.add_argument("--retry-case")
            child.add_argument("--reason")
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    try:
        config = Trial.model_validate_json(args.config.read_bytes())
        store = Store(args.output_dir)
        with store.lock():
            prepare(store, config)
        if args.operation == "run":
            run(
                store,
                config,
                RawAudioTransport(timeout_seconds=config.timeout_seconds),
                case_id=args.case_id,
                retry_case=args.retry_case,
                reason=args.reason,
            )
        with store.lock():
            report = export(store, config)
        # Provider raw bodies and error messages stay in private artifacts, never stdout.
        summary = {
            key: report[key]
            for key in (
                "status",
                "requests_submitted",
                "credits_or_reservation",
                "credit_cap",
                "qualification_status",
                "production_ready",
                "report_path",
            )
        }
        print(json.dumps(summary), flush=True)
        return 0 if args.operation != "run" or report["status"] == "responses_saved" else 2
    except (ContractError, Conflict) as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    except (ValueError, OSError, ValidationError):
        print(json.dumps(dict(ok=False, code="toneperfect_invalid_local_input")), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
