"""Validate the editor's source selectors against the full frozen text representations."""

import hashlib
from collections.abc import Callable

from .contracts.common import TextSelector, select_text
from .errors import ContractError


def verify_excerpts(rows: list, read_text: Callable[[str], str]) -> None:
    if not isinstance(rows, list):
        raise ContractError("evidence_fixture_must_be_array")
    labels = set()
    representations = {}
    for row in rows:
        label = row["label"]
        if not label:
            raise ContractError("empty_evidence_label")
        if label in labels:
            raise ContractError("duplicate_evidence_label", label)
        labels.add(label)
        filename = row["representation"]
        if filename not in representations:
            representations[filename] = read_text(filename)
        selector = TextSelector(
            kind="text",
            block_ids=[],
            char_start=row["start_codepoint"],
            char_end=row["end_codepoint"],
            exact=row["exact"],
            prefix="",
            suffix="",
        )
        if not selector.exact:
            raise ContractError("empty_evidence_excerpt", label)
        select_text(representations[filename], selector)
        if hashlib.sha256(selector.exact.encode()).hexdigest() != row["exact_sha256"]:
            raise ContractError("evidence_hash_mismatch", label)
