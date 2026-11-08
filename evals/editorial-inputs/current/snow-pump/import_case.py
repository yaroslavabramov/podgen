"""Admit the current case and its exact files through ordinary import commands; no models."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from podgen.writing_cli import main as writing_main

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    prefix = [
        "--project",
        str(args.project),
        "--actor",
        str(args.actor),
        "--objects",
        str(args.objects),
    ]

    def invoke(command, **options):
        values = [*prefix, command]
        for key, value in options.items():
            values.extend(["--" + key.replace("_", "-"), str(value)])
        code = writing_main(values)
        if code:
            raise SystemExit(code)

    packet = json.loads((HERE / "shared.json").read_text())
    mapping = {}
    for document in packet["documents"]:
        source_path = document["source_path"]
        base = (
            ROOT / "evals"
            if source_path.startswith("editorial-inputs/")
            else ROOT / "evals/cases/development/wp02-batch-01"
        )
        output = args.out_dir / (document["key"] + "-ref.json")
        invoke(
            "editorial-source-import",
            file=base / source_path,
            source_path=source_path,
            purpose=document["purpose"],
            scope=args.scope,
            out=output,
        )
        mapping[source_path] = json.loads(output.read_text())
    source_map = args.out_dir / "source-map.json"
    source_map.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")
    invoke(
        "editorial-import",
        input=HERE / "shared.json",
        source_map=source_map,
        out=args.out_dir / "shared-ref.json",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
