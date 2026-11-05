"""Bridge editor extraction fixtures to the canonical Unicode selector contract."""

import argparse
import json
from pathlib import Path

from podgen.domain.frozen_evidence import verify_excerpts
from podgen.domain.packages import relative_path


def check(path: Path):
    rows = json.loads(path.read_bytes().decode("utf-8"))
    verify_excerpts(rows, lambda name: relative_path(path.parent, name).read_bytes().decode("utf-8"))
    return dict(
        file=str(path),
        checked_spans=len(rows),
        checks=["code point selectors", "full exact text", "SHA-256"],
        pending=[
            "canonical SourceRevision/Representation refs and import package",
            "original anchor and semantic support review",
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.evidence:
        print(json.dumps(check(path), ensure_ascii=False))
