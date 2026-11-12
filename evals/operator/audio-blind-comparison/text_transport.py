"""Diagnostic text-only Realtime exchange; no production transport changes."""

import asyncio
import json
import time

from podgen.audio.fidelity.adjudication.realtime import URL
from podgen.audio.realtime_transport import OriginBoundConnect, RealtimeReceipt
from podgen.domain.canonical import canonical_bytes


async def submit_text(events, credential):
    transcript, socket, status, headers = [], None, None, {}
    started = time.monotonic()
    error, done = None, False

    async def receive(kind):
        while True:
            raw = await socket.recv()
            transcript.append(dict(direction="server", raw=raw))
            event = json.loads(raw)
            if event.get("type") == "error":
                raise RuntimeError("provider_error")
            if event.get("type") == kind:
                return event

    async def send(event):
        raw = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        transcript.append(dict(direction="client", raw=raw))
        await socket.send(raw)

    try:
        async with asyncio.timeout(180):
            socket = await OriginBoundConnect(
                URL, additional_headers={"Authorization": "Bearer " + credential},
                open_timeout=180, close_timeout=0, compression=None, max_size=None, proxy=None,
            )
            status = socket.response.status_code
            headers = {k.lower(): v for k, v in socket.response.headers.items()
                       if k.lower() != "set-cookie"}
            await receive("session.created")
            await send(events[0])
            await receive("session.updated")
            await send(events[1])
            await receive("conversation.item.done")
            await send(events[2])
            await receive("response.done")
            done = True
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if socket is not None:
            socket.transport.abort()
    return RealtimeReceipt(status, headers, canonical_bytes(dict(events=transcript)),
                           round((time.monotonic() - started) * 1000), done, done, error)
