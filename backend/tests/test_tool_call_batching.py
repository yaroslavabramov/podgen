"""New durable research contracts may batch tool calls; saved contracts keep one call per turn."""


def test_new_research_tools_batch_calls_and_old_contracts_keep_one_call_per_turn():
    from podgen.runtime.evaluation.source_tool_contract import BATCH_KEY, BATCH_VERSION, batched
    from podgen.runtime.models.messages_wire import messages_request

    assert batched({BATCH_KEY: BATCH_VERSION}) and not batched({}) and not batched(None)
    body = dict(
        model="claude-opus-5-5",
        instructions="Read.",
        input=[dict(role="user", content=[dict(type="input_text", text="{}")])],
        text=dict(format=dict(type="json_schema", name="out", schema=dict(type="object"), strict=True)),
        reasoning=dict(effort="medium"),
        tools=[
            dict(type="function", name="read_source", description="Read.", parameters=dict(type="object"))
        ],
        tool_choice="auto",
        max_output_tokens=10,
        metadata={},
    )
    for parallel in (False, True):
        wire = messages_request(dict(body, parallel_tool_calls=parallel))
        assert wire["tool_choice"] == dict(type="auto", disable_parallel_tool_use=not parallel)
