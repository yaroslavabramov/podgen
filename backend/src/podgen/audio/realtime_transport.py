"""One origin-bound WebSocket exchange under a single total deadline."""

import asyncio
import json
import time
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from podgen.audio.fidelity.adjudication.realtime import URL
from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

from .realtime_protocol import ProviderEventError, exchange


class OriginBoundConnect(connect):
    def process_redirect(self, exc):
        return exc


@dataclass(frozen=True)
class RealtimeReceipt:
    handshake_status: int | None
    handshake_headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    complete: bool
    response_done: bool
    error: str | None


class RealtimeTransport:
    def __init__(self, *, timeout_seconds, connector=None):
        self.timeout_seconds = timeout_seconds
        self.connector = connector or OriginBoundConnect

    def submit(self, request, wire, credential):
        if request.get("url") != URL or request.get("transport") != "websocket":
            raise ContractError("realtime_unrecognized_provider_endpoint")
        return asyncio.run(self._submit(wire, credential))

    async def _submit(self, wire, credential):
        started = time.monotonic()
        socket, status, headers, transcript = None, None, {}, []
        complete, done, error = False, False, None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                socket = await self.connector(
                    URL,
                    additional_headers={"Authorization": "Bearer " + credential},
                    open_timeout=self.timeout_seconds,
                    close_timeout=0,
                    compression=None,
                    max_size=None,
                    proxy=None,
                )
                status = socket.response.status_code
                headers = {
                    k.lower(): v for k, v in socket.response.headers.items() if k.lower() != "set-cookie"
                }
                if status != 101:
                    raise ContractError("realtime_handshake_not_upgraded")
                await exchange(socket, json.loads(wire), transcript)
                complete, done = True, True
        except InvalidStatus as exc:
            status = exc.response.status_code
            headers = {k.lower(): v for k, v in exc.response.headers.items() if k.lower() != "set-cookie"}
            transcript.append(dict(direction="handshake", raw=bytes(exc.response.body).hex()))
            complete, error = True, type(exc).__name__
        except ProviderEventError as exc:
            complete, error = True, type(exc).__name__
        except (TimeoutError, OSError, WebSocketException, ValueError, ContractError) as exc:
            error = type(exc).__name__
        finally:
            if socket is not None:
                # Abort is synchronous, so cleanup cannot extend the total network deadline.
                socket.transport.abort()
        return RealtimeReceipt(
            status,
            headers,
            canonical_bytes(dict(events=transcript)),
            round((time.monotonic() - started) * 1000),
            complete,
            done,
            error,
        )
