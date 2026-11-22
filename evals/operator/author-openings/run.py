"""Prepare / live / replay; only explicit live performs requests, never production runs."""

import argparse
import secrets
from pathlib import Path

from artifacts import read, save, send_once
from conditions import author_request, prepare
from providers import Transport, credentials, select
from results import replay

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    preparation = actions.add_parser("prepare")
    preparation.add_argument(
        "--draft-request", type=Path, required=True, help="Saved request next to its receipt.json"
    )
    preparation.add_argument("--accepted-plan", type=Path, required=True)
    preparation.add_argument("--pricing", type=Path, required=True, help="Rate snapshot used for the trial")
    for action in (preparation, actions.add_parser("live"), actions.add_parser("replay")):
        action.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "prepare":
        rates = read(args.pricing)
        prepare(
            args.draft_request, args.draft_request.with_name("receipt.json"), directory, args.accepted_plan
        )
        save(directory / "pricing.json", rates)
        print("Prepared immutable common conditions; no generation calls.")
        return
    if args.action == "replay":
        print(replay(directory))
        return
    original = read(directory / "source/request.json")
    common = read(directory / "common/payload.json")
    transports = {provider: Transport(provider, key) for provider, key in credentials(ROOT).items()}
    selected = select(directory, transports)
    identity_path = directory / "sealed/identity.json"
    if not identity_path.exists():
        entries = [dict(slot="candidate-" + str(index), **entry) for index, entry in enumerate(selected, 1)]
        secrets.SystemRandom().shuffle(entries)
        save(identity_path, dict(zip(("A", "B"), entries, strict=True)))
    identity = read(identity_path)
    # Persist BOTH author requests before either response can influence the experiment.
    for entry in identity.values():
        body = author_request(original, common, entry["provider"], entry["model"])
        save(directory / "sealed/candidates" / entry["slot"] / "request.json", body)
    for entry in identity.values():
        folder = directory / "sealed/candidates" / entry["slot"]
        send_once(folder, read(folder / "request.json"), transports[entry["provider"]])
    print(replay(directory))


if __name__ == "__main__":
    main()
