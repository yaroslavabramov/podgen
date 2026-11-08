"""Prepare / live / replay; only explicit live performs requests, never production runs."""

import argparse
import secrets
from pathlib import Path

from artifacts import read, save, send_once
from conditions import author_request, prepare
from providers import Transport, credentials, select
from results import replay

ROOT = Path(__file__).resolve().parents[3]
DEFAULT = ROOT / "evals/reports/2026-11-08-author-openings"
SOURCE = ROOT / "tmp/snow-edit-nov7/continued-native"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "live", "replay"])
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "prepare":
        request = next(SOURCE.glob("draft-*/request.json"))
        prepare(request, request.with_name("receipt.json"), directory, SOURCE / "export/plan-revise.json")
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
