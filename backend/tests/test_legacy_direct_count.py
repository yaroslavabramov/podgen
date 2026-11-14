"""Legacy-shaped direct receipts retain JSON types when validating archived proof."""

import io
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.compiler import count_request, response_request
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.models.count_receipts import identity, native_count
from podgen.runtime.models.legacy_direct_count import LegacyDirectCount


@pytest.fixture
def legacy_direct(repo, scope):
    project = scope[0]
    profile_ref = create_profile(
        repo, project, model="gpt-6.1-sol", context_limit=100000, output_limit=1000
    ).ref
    profile = load_profile(repo, project, profile_ref, "draft")
    body = response_request(
        uuid4(),
        "draft",
        profile,
        dict(synthetic="legacy direct count"),
        "Synthetic task",
        dict(
            type="object",
            properties=dict(flag=dict(enum=[True])),
            required=["flag"],
            additionalProperties=False,
        ),
    )
    invocation_id = body["metadata"]["podgen_invocation_id"]

    def blob(value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        return repo.save_blob(project, io.BytesIO(raw), "application/json").model_dump(mode="json")

    def record(payload):
        reference = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(project, reference, payload, audience="evaluator")
        return reference

    request = record(
        dict(
            contract="model-request-v1",
            invocation_id=invocation_id,
            run_id=body["metadata"]["podgen_run_id"],
            stage="draft",
            request_blob=blob(body),
            context=dict(profile_ref=profile_ref.model_dump(mode="json")),
        )
    )

    def event(kind, **fields):
        return record(
            dict(
                contract="model-invocation-event-v1",
                invocation_id=invocation_id,
                request_ref=request.model_dump(mode="json"),
                event_kind=kind,
                **fields,
            )
        )

    # Compatibility proof uses the original shapes, without adding a v3 count
    # contract, a new native-request field or a rewritten historical receipt.
    intent = event("count_intent", data=dict(request_blob=blob(count_request(body))))
    receipt = event(
        "count_response",
        data={},
        provider_http_status=200,
        response_blob=blob(dict(input_tokens=100)),
        wire_request_blob=blob(native_count(body, profile.provider)),
        secret_redacted=False,
    )
    return SimpleNamespace(
        journal=EvaluationJournal(repo, project),
        body=body,
        profile=profile,
        intent=intent,
        receipt=receipt,
        references=(request, intent, receipt),
    )


@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "boolean-to-integer"])
def test_legacy_direct_count_rejects_boolean_integer_schema_alias(legacy_direct, changed):
    case = legacy_direct
    before = {ref: json.dumps(case.journal.read(ref), sort_keys=True) for ref in case.references}
    body = deepcopy(case.body)
    if changed:
        body["text"]["format"]["schema"]["properties"]["flag"]["enum"] = [1]
        # Python equality masked this change in all three archived request checks.
        assert body == case.body
        assert identity(body, case.profile) != identity(case.body, case.profile)
    # Recovery derives this handle's identity from the current request. Comparing
    # it with itself cannot replace authentication against the original archive.
    receipt = LegacyDirectCount(case.receipt, identity(body, case.profile), 100, case.intent)
    if changed:
        with pytest.raises(Conflict, match="legacy_count_receipt_request_changed"):
            receipt.validate(case.journal, body, case.profile)
    else:
        assert receipt.validate(case.journal, body, case.profile) == 100
    assert {ref: json.dumps(case.journal.read(ref), sort_keys=True) for ref in case.references} == before
