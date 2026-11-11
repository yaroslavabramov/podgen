"""Repair attempts retain the original commission and reject crossed request lineage."""

from copy import deepcopy
from uuid import uuid4

import pytest

from podgen.domain.errors import Conflict, ContractError
from podgen.runtime.evaluation import recheck_source
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing.revision import verification
from tests.recheck_repair_cases import ref, repaired_case


def verify(case, version, monkeypatch):
    journal, root, leaf, script, comparison, output = case
    monkeypatch.setattr(recheck_source, "load", lambda *args, **kwargs: (script, comparison))
    context = journal.records[leaf["request_record_id"]]["context"]
    recheck_source.verify_saved(
        journal,
        root["run_id"],
        root["stage"],
        context["script_ref"],
        script,
        leaf,
        context,
        output,
        version=version,
    )


@pytest.mark.parametrize("version", [verification.VERSION, verification.TASK_VERSION])
@pytest.mark.parametrize("retry", [False, True])
def test_native_repaired_recheck_and_technical_retry_bind_to_original_request(monkeypatch, version, retry):
    case = repaired_case(version, retry)
    journal, root, leaf, _, comparison, _ = case
    before = deepcopy(journal.records)
    assert request_payload(journal.request(root))["revision_check"] == comparison
    # This is the actual short repair payload that previously caused binding_mismatch.
    assert "revision_check" not in request_payload(journal.request(leaf))
    verify(case, version, monkeypatch)
    assert journal.records == before


@pytest.mark.parametrize(
    "wrong",
    [
        "parent",
        "cycle",
        "parent-index",
        "root-run",
        "root-stage",
        "root-operation",
        "root-envelope",
        "request-ref",
        "context-parent",
        "binding",
        "repair-prose",
    ],
)
def test_wrong_repair_parent_root_request_or_result_is_rejected(monkeypatch, wrong):
    case = repaired_case(verification.TASK_VERSION)
    journal, root, leaf, _, _, output = case
    record = journal.records[leaf["request_record_id"]]
    if wrong == "parent":
        leaf["parent_invocation_id"] = uuid4()
    elif wrong == "cycle":
        leaf["parent_invocation_id"] = leaf["invocation_id"]
    elif wrong == "parent-index":
        leaf["parent_attempt_index"] = leaf["attempt_index"]
    elif wrong == "root-run":
        root["run_id"] = uuid4()
    elif wrong == "root-stage":
        root["stage"] = "reader-review-2"
    elif wrong == "root-operation":
        root["operation_kind"] = "anchor_repair"
    elif wrong == "root-envelope":
        journal.records[root["request_record_id"]]["invocation_id"] = str(uuid4())
    elif wrong == "request-ref":
        record["parent_request_ref"] = ref(uuid4())
    elif wrong == "context-parent":
        record["context"]["parent_request_ref"] = ref(uuid4())
    elif wrong == "binding":
        record["context"]["revision_verification"]["comparison_hash"] = "wrong"
    elif wrong == "repair-prose":
        output["listener_answer"] = "An uncommissioned new judgment."
    with pytest.raises((Conflict, ContractError)):
        verify(case, verification.TASK_VERSION, monkeypatch)
