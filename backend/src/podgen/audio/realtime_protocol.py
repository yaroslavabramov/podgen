"""One committed audio turn, with explicit acknowledgement barriers."""

import json

from podgen.audio.fidelity.adjudication.realtime import TEXT_ITEM_ID
from podgen.domain.errors import ContractError


class ProviderEventError(Exception):
    pass


async def exchange(socket, events, transcript):
    async def receive(kind, predicate=lambda event: True):
        while True:
            raw = await socket.recv()
            if not isinstance(raw, str):
                transcript.append(dict(direction="server_binary", raw=bytes(raw).hex()))
                raise ContractError("realtime_unexpected_binary_event")
            transcript.append(dict(direction="server", raw=raw))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ContractError("realtime_invalid_server_event")
            if value.get("type") == "error":
                raise ProviderEventError("realtime_provider_error_event")
            if value.get("type") == "response.done" and kind != "response.done":
                raise ContractError("realtime_response_before_committed_turn")
            if value.get("type") == kind and predicate(value):
                return value

    async def send(event):
        raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        # Preserve an attempted send even when acknowledgement is lost.
        transcript.append(dict(direction="client", raw=raw))
        await socket.send(raw)

    await receive("session.created")
    await send(events[0])
    await receive("session.updated")
    await send(events[1])
    await receive("conversation.item.done", lambda e: e.get("item", {}).get("id") == TEXT_ITEM_ID)
    await send(events[2])
    await send(events[3])
    await receive("input_audio_buffer.committed")
    await send(events[4])
    return await receive("response.done")
