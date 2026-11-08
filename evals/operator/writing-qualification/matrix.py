"""Run one independently journaled qualification trial, or import its source packets."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid5

from dotenv import dotenv_values

from podgen.runtime.evaluation.planning_continuation.source import continuation_id

ROOT = Path(__file__).resolve().parents[3]
CASES = {
    "snow-pump": "evals/editorial-inputs/current/snow-pump/shared.json",
    "gps-clock": "evals/editorial-inputs/current/gps-clock/shared.json",
    "wright-first-flight": "evals/reports/2026-11-08-story-editing/transfer/input-corrected/shared.json",
    "rain-soil": "evals/editorial-inputs/experiments/rain-soil/shared.json",
}
PROFILES = {
    "planner": ("openai", "gpt-6.1-sol", ["planner"]),
    "author": ("anthropic", "claude-opus-5-5", ["writer"]),
    "editor": ("openai", "gpt-6.1-terra", ["plan_reviewer", "editorial_reviewer"]),
}


def read(path):
    return json.loads(path.read_bytes())


def save(path, obj):
    content = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text() != content:
        raise ValueError(f"Conflicting prior output at {path}")
    path.write_text(content)


def at(path):
    return "@" + str(path)


class Matrix:
    def __init__(self, args):
        self.workspace = args.workspace.resolve()
        state = read(self.workspace / "db-state.json")
        self.project = UUID(state["project"])
        config = dotenv_values(args.env_file) if args.env_file else {}
        self.env = {
            **{
                k: v
                for k, v in config.items()
                if k in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} and v
            },
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend/src"),
            "PODGEN_DATABASE_URL": "postgresql+psycopg://runner@/postgres?host="
            + state["socket"]
            + "&port=55449",
        }
        self.prefix = [
            sys.executable,
            "-m",
            "podgen.writing_cli",
            "--project",
            state["project"],
            "--actor",
            state["actor"],
            "--objects",
            str(self.workspace / "objects"),
        ]

    def cli(self, *options, log=None):
        result = subprocess.run(
            self.prefix + list(map(str, options)),
            env=self.env,
            cwd=ROOT,
            stdout=log or subprocess.DEVNULL,
            stderr=log,
            check=False,
        )
        if log is None:
            result.check_returncode()
        return result.returncode

    def prepare(self):
        for name, (provider, model, roles) in PROFILES.items():
            dest = self.workspace / f"profile-{name}.json"
            if not dest.exists():
                self.cli(
                    "profile-create",
                    "--model",
                    model,
                    "--provider",
                    provider,
                    "--context-limit",
                    200000,
                    "--output-limit",
                    24576,
                    "--reasoning-effort",
                    "medium",
                    "--verbosity",
                    "high",
                    "--roles",
                    *roles,
                    "--operation-id",
                    uuid5(self.project, name),
                    "--out",
                    dest,
                )
        for case, relative in CASES.items():
            packet = ROOT / relative
            refdir = self.workspace / "data" / case
            refdir.mkdir(parents=True, exist_ok=True)
            refs = {}
            for doc in read(packet)["documents"]:
                path = doc["source_path"]
                candidates = [
                    packet.parent / path,
                    ROOT / "evals" / path,
                    ROOT / "evals/cases/development/wp02-batch-01" / path,
                    ROOT / path,
                ]
                source = next(
                    p
                    for p in candidates
                    if p.is_file()
                    and hashlib.sha256(p.read_bytes()).hexdigest() == doc["sha256"]
                )
                ref = refdir / (doc["key"] + ".json")
                if not ref.exists():
                    self.cli(
                        "editorial-source-import",
                        "--file",
                        source,
                        "--source-path",
                        path,
                        "--scope",
                        case,
                        "--purpose",
                        doc["purpose"],
                        "--out",
                        ref,
                    )
                refs[path] = read(ref)
            save(refdir / "source-map.json", refs)
            if not (refdir / "shared-ref.json").exists():
                self.cli(
                    "editorial-import",
                    "--input",
                    packet,
                    "--source-map",
                    refdir / "source-map.json",
                    "--out",
                    refdir / "shared-ref.json",
                )

    def execute(self, trial, identifier, args, log):
        export = trial / str(identifier)
        if not (export / "run.json").exists():
            result = self.cli(
                *args, "--run-id", identifier, "--export-dir", export, log=log
            )
            if not (export / "run.json").exists():
                raise RuntimeError(
                    f"Run {identifier} exited {result} before export; inspect its log and journal."
                )
        return export, read(export / "run.json")

    def trial(self, case, release, repeat):
        label = release.split("/")[0] + "-" + case + "-" + str(repeat)
        folder = self.workspace / "trials" / label
        folder.mkdir(parents=True, exist_ok=True)
        profile = lambda role: at(self.workspace / f"profile-{role}.json")
        common = [
            "run",
            "--case",
            case,
            "--author-ref",
            at(self.workspace / "data" / case / "shared-ref.json"),
            "--behavior-profile",
            release,
            "--writer-profile",
            profile("author"),
            "--reviewer-profile",
            profile("editor"),
            "--review-anchor-policy",
            "bounded-selection-v1",
            "--max-total-input-tokens",
            600000,
            "--max-total-output-tokens",
            300000,
            "--max-elapsed-seconds",
            14400,
            "--repeat-index",
            repeat,
        ]
        with (folder / "operator.log").open("a", buffering=1) as log:
            first = uuid5(self.project, label + ":1")
            original, result = self.execute(
                folder,
                first,
                [
                    *common,
                    "--plan-mode",
                    "own-plan",
                    "--planner-profile",
                    profile("planner"),
                    "--plan-reviewer-profile",
                    profile("editor"),
                    "--max-plan-revisions",
                    1,
                    "--editorial-pass",
                    1,
                    "--max-script-revisions",
                    2,
                ],
                log,
            )
            sessions = [original.name]
            if result.get("failure_code") == "planning_revision_limit_exhausted":
                original, result = self.execute(
                    folder,
                    continuation_id(first),
                    [
                        "continue-planning",
                        "--source-run-id",
                        first,
                        "--max-additional-plan-revisions",
                        2,
                    ],
                    log,
                )
                sessions.append(original.name)
            current = original
            for editorial_pass, edits in [(2, 1), (3, 2)]:
                if result.get("review_status") != "revision_limit_exhausted":
                    break
                outcome = read(current / "review-outcome.json")
                plan = folder / "accepted-plan.json"
                if not plan.exists():
                    self.cli(
                        "accepted-plan-export", "--run-id", original.name, "--out", plan
                    )
                current, result = self.execute(
                    folder,
                    uuid5(self.project, f"{label}:{editorial_pass}"),
                    [
                        *common,
                        "--plan-mode",
                        "fixed-plan",
                        "--fixed-plan-input-ref",
                        at(plan),
                        "--editorial-pass",
                        editorial_pass,
                        "--max-script-revisions",
                        edits,
                        "--initial-script-ref",
                        json.dumps(outcome["script_ref"]),
                    ],
                    log,
                )
                sessions.append(current.name)
            save(
                folder / "terminal.json",
                dict(
                    case=case,
                    release=release,
                    repeat=repeat,
                    sessions=sessions,
                    final=current.name,
                    status=result["status"],
                    editorial=result.get("review_status"),
                    failure=result.get("failure_code"),
                ),
            )
        print(
            label,
            result["status"],
            result.get("review_status"),
            result.get("failure_code"),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    trial = sub.add_parser("trial")
    trial.add_argument(
        "release", choices=("narrative-rebuild/1", "editorial-candidate/1")
    )
    trial.add_argument("case", choices=CASES)
    trial.add_argument("repeat", type=int, choices=(1, 2))
    args = parser.parse_args()
    matrix = Matrix(args)
    if args.command == "prepare":
        matrix.prepare()
    else:
        matrix.trial(args.case, args.release, args.repeat)
