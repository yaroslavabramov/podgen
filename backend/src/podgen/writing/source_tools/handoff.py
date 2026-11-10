"""Bind model-cited reads to actual tool deliveries and carry only cited passages into required edits."""

import re
from copy import deepcopy

from podgen.domain.errors import ContractError

from .corpus import SourceCorpus


def resolve_review_reads(output, successful_read_passages, materials):
    review = deepcopy(output)
    if not any(finding.get("source_read_ids") for finding in review["findings"]):
        return review
    corpus = SourceCorpus(materials)
    delivered = {}
    for passage in successful_read_passages:
        alias = passage["read_id"]
        doc = corpus.documents.get(passage["source_id"])
        if doc is None or not re.fullmatch(r"E[1-9]\d*", alias) or alias in delivered:
            raise ContractError("invalid_source_read_receipt")
        locator = f"{passage['source_id']}:{doc.digest}:{passage['start']}:{passage['end']}"
        if corpus.locate(locator) != {key: value for key, value in passage.items() if key != "read_id"}:
            raise ContractError("source_read_receipt_mismatch")
        delivered[alias] = locator
    for finding in review["findings"]:
        ids = finding["source_read_ids"]
        if len(ids) != len(set(ids)) or any(alias not in delivered for alias in ids):
            raise ContractError("unread_or_duplicate_source_reference")
        finding["source_read_ids"] = [delivered[alias] for alias in ids]
    return review


def attach_task_passages(payload, materials):
    """Intern shared passages once per rewrite. Internal locators never enter the model request."""
    tasks = payload["revision_tasks"]
    if not any(task.get("source_read_ids") for task in tasks):
        return
    corpus = SourceCorpus(materials)
    refs = {}
    passages = []
    for task in tasks:
        locators = task.pop("source_read_ids", ())
        if not locators:
            continue
        aliases = []
        for locator in locators:
            if locator not in refs:
                alias = f"P{len(refs) + 1}"
                refs[locator] = alias
                passages.append(dict(ref=alias, **corpus.locate(locator)))
            aliases.append(refs[locator])
        task["source_read_refs"] = aliases
    payload["review_source_passages"] = passages
