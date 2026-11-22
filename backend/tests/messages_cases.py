"""A complete author request for transport and token-count tests."""


def author_request():
    return dict(
        model="claude-test",
        instructions="Write the episode.",
        metadata={"podgen_stage": "draft"},
        input=[dict(role="user", content=[dict(type="input_text", text="A short brief.")])],
        reasoning=dict(effort="medium"),
        text=dict(format=dict(type="json_schema", name="script", schema=dict(type="object", properties={}))),
        tools=[],
        tool_choice="none",
        max_output_tokens=256,
    )
