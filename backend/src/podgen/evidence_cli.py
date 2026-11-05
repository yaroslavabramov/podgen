"""Bridge editor extraction fixtures to the canonical Unicode selector contract."""

import argparse
import hashlib
import json
from pathlib import Path

from podgen.domain.contracts.common import TextSelector, select_text
from podgen.domain.errors import ContractError
from podgen.domain.packages import relative_path


def check(path: Path):
    rows = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(rows, list):
        raise ContractError("evidence_fixture_must_be_array")
    labels = set()
    for row in rows:
        if row["label"] in labels:
            raise ContractError("duplicate_evidence_label")
        labels.add(row["label"])
        text = relative_path(path.parent, row["representation"]).read_bytes().decode("utf-8")
        selector = TextSelector(
            kind="text",
            block_ids=[],
            char_start=row["start_codepoint"],
            char_end=row["end_codepoint"],
            exact=row["exact"],
            prefix="",
            suffix="",
        )
        select_text(text, selector)
        if hashlib.sha256(selector.exact.encode()).hexdigest() != row["exact_sha256"]:
            raise ContractError("evidence_hash_mismatch", row["label"])
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
