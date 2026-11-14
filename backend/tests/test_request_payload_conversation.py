"""Decode the semantic commission from a saved tool conversation without rewriting it."""

import json
from copy import deepcopy

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing.prompt_cache import VERSIONS


def saved_form(version):
    # Matches the failed credit-recovery request's input-item forms; no live IDs/data.
    return dict(
        metadata=dict(podgen_prompt_cache_layout=version),
        input=[
            dict(
                role="user",
                content=[
                    dict(
                        type="input_text",
                        text=json.dumps(
                            dict(script=dict(utterances=[dict(index=0, text="Exact speech.\n😀")]))
                        ),
                    ),
                    dict(
                        type="input_text",
                        text=json.dumps(dict(factual_remit=dict(question="Exact commission"))),
                    ),
                ],
            ),
            dict(type="reasoning", id="reasoning-1", summary=[]),
            dict(
                type="function_call",
                call_id="call-1",
                name="read_source",
                arguments='{"source_id":"S3","start":0,"end":200}',
            ),
            dict(
                type="function_call_output", call_id="call-1", output='{"text":"Complete retained evidence"}'
            ),
            dict(
                type="reasoning",
                id="reasoning-2",
                summary=[dict(type="summary_text", text="Compare the passage")],
            ),
            dict(
                type="function_call",
                call_id="call-2",
                name="search_sources",
                arguments='{"query":"precise fact"}',
            ),
            dict(type="function_call_output", call_id="call-2", output='{"matches":[]}'),
        ],
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_saved_recovery_conversation_preserves_exact_commission_and_request(version):
    body = saved_form(version)
    before = deepcopy(body)
    ordinary = dict(body, input=deepcopy(body["input"][:1]))
    assert (
        request_payload(body)
        == request_payload(ordinary)
        == dict(
            script=dict(utterances=[dict(index=0, text="Exact speech.\n😀")]),
            factual_remit=dict(question="Exact commission"),
        )
    )
    assert body == before


def test_anthropic_assistant_items_and_normal_messages_are_not_user_commissions():
    body = saved_form(VERSIONS[0])
    body["input"].extend(
        [
            dict(
                type="anthropic_assistant",
                content=[dict(type="tool_use", id="call-3", name="read_source", input={})],
            ),
            dict(role="assistant", content=[dict(type="output_text", text="Prior assistant message")]),
        ]
    )
    assert request_payload(body) == request_payload(dict(body, input=body["input"][:1]))


@pytest.mark.parametrize("item", [{"type": "message"}, {"type": "unknown_item"}, {"role": "unknown"}, "bad"])
def test_unknown_items_are_rejected_instead_of_silently_dropping_them(item):
    body = saved_form(VERSIONS[0])
    body["input"].append(item)
    with pytest.raises(ContractError, match="invalid_multipart_input_item"):
        request_payload(body)


def test_duplicate_user_commission_and_malformed_content_are_not_hidden():
    body = saved_form(VERSIONS[0])
    body["input"].append(deepcopy(body["input"][0]))
    with pytest.raises(ContractError, match="invalid_multipart_user_payload"):
        request_payload(body)
    body["input"] = [dict(role="user")]
    with pytest.raises(KeyError, match="content"):
        request_payload(body)


def test_old_single_part_request_decoding_is_unchanged():
    body = dict(
        input=[
            dict(
                role="user", content=[dict(type="input_text", text='{"script":{"text":"Exact old request"}}')]
            )
        ]
    )
    before = deepcopy(body)
    assert request_payload(body) == dict(script=dict(text="Exact old request"))
    assert body == before
