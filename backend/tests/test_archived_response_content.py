import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.rejected_output import saved_text
from podgen.runtime.models.response_content import output_text


@pytest.mark.parametrize("offered", [True, False])
def test_readmission_reuses_response_content_under_the_original_tool_contract(offered):
    body = dict(
        model="reviewer", metadata={"role": "fact"}, tools=[{"type": "web_search"}] if offered else []
    )
    row = dict(status="validation_failed", operation_kind="role", provider_response_id="response")
    response = dict(
        id="response",
        model="reviewer",
        status="completed",
        metadata=body["metadata"],
        output=[
            dict(type="web_search_call", status="completed", action=dict(type="search", query="inscription")),
            dict(type="message", role="assistant", content=[dict(type="output_text", text='{"result":1}')]),
        ],
    )
    saved = json.dumps(response).encode()
    event = dict(record_id=uuid4(), event_kind="source_tools_final_response")
    journal = SimpleNamespace(
        request=lambda row: body,
        events=lambda row: [event],
        read=lambda ref: dict(
            response_blob=dict(
                storage_key="body", sha256="0" * 64, byte_length=len(saved), media_type="application/json"
            )
        ),
        read_blob=lambda *args: saved,
    )
    if offered:
        assert (
            saved_text(journal, row, code="rejected")[0]
            == output_text(response, web_search=True)
            == '{"result":1}'
        )
    else:
        with pytest.raises(RunFailure, match="rejected"):
            saved_text(journal, row, code="rejected")
