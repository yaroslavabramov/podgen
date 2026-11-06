"""Offline packet authoring. New outputs are exclusive; existing sources are never overwritten."""

import argparse
import hashlib
import json
from pathlib import Path

from podgen.runtime.evaluation.validation import load_unique_json
from podgen.writing.card_authoring import add_card, rebind_fixed, upgrade_fixed, upgrade_shared


def write_new(outputs):
    created = []
    try:
        if len({path.resolve() for path, _ in outputs}) != len(outputs):
            raise ValueError("editorial_output_paths_must_differ")
        for path, _ in outputs:
            if path.exists() or path.is_symlink():
                raise FileExistsError(path)
        for path, data in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                created.append(path)
                handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        for path in created:
            path.unlink()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("upgrade", "upgrade-fixed", "add-card", "rebind-fixed"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "upgrade":
            command.add_argument("--map-out", type=Path, required=True)
        elif name == "upgrade-fixed":
            command.add_argument("--shared", type=Path, required=True)
        elif name == "add-card":
            command.add_argument("--card", type=Path, required=True)
        else:
            command.add_argument("--shared", type=Path, required=True)
            command.add_argument("--previous-shared", type=Path, required=True)
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    data = load_unique_json(raw)
    if args.command == "upgrade":
        result, mapping = upgrade_shared(data)
        mapping["source_sha256"] = hashlib.sha256(raw).hexdigest()
        write_new([(args.out, result), (args.map_out, mapping)])
    elif args.command == "upgrade-fixed":
        write_new([(args.out, upgrade_fixed(data, load_unique_json(args.shared.read_bytes())))])
    elif args.command == "add-card":
        result, identity = add_card(data, load_unique_json(args.card.read_bytes()))
        write_new([(args.out, result)])
        print(json.dumps(dict(card_id=identity)))
    else:
        write_new(
            [
                (
                    args.out,
                    rebind_fixed(
                        data,
                        load_unique_json(args.previous_shared.read_bytes()),
                        load_unique_json(args.shared.read_bytes()),
                    ),
                )
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
