import asyncio
import json
import time

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from podgen.audio.fidelity.adjudication.realtime import URL, parse, prepare
from podgen.audio.realtime_transport import OriginBoundConnect, RealtimeTransport
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.adjudication.contracts import RealtimeReviewerProfile
from tests.audio_cases import signal
from tests.audio_realtime_cases import Connector, Socket


def prepared():
    return prepare(
        "Neutral role.",
        dict(output_language="ru", cases=[dict(case_id="C1")]),
        signal(),
        RealtimeReviewerProfile(),
    )


def test_real_protocol_barriers_auth_and_transcript_are_preserved():
    request, wire = prepared()
    socket = Socket()
    connector = Connector(socket)
    receipt = RealtimeTransport(timeout_seconds=1, connector=connector).submit(request, wire, "openai-secret")
    assert (
        receipt.handshake_status == 101 and receipt.complete and receipt.response_done and not receipt.error
    )
    assert len(connector.calls) == 1 and socket.aborted
    assert connector.calls[0][0] == URL
    assert connector.calls[0][1]["additional_headers"] == {"Authorization": "Bearer openai-secret"}
    assert socket.sent == json.loads(wire)
    assert parse(receipt.body, ["C1"], 5)[0].results[0].status == "dismissed"
    assert "openai-secret" not in receipt.body.decode()
    assert socket.received.index("conversation.item.done") < socket.received.index(
        "input_audio_buffer.committed"
    )


@pytest.mark.parametrize(
    "stage",
    [
        "session.created",
        "session.updated",
        "conversation.item.done",
        "input_audio_buffer.committed",
        "response.done",
    ],
)
def test_deadline_covers_every_ack_without_repeats(stage):
    request, wire = prepared()
    socket = Socket(stall=stage)
    connector = Connector(socket)
    started = time.monotonic()
    receipt = RealtimeTransport(timeout_seconds=0.03, connector=connector).submit(request, wire, "key")
    assert time.monotonic() - started < 0.3
    assert not receipt.complete and not receipt.response_done and receipt.error == "TimeoutError"
    assert socket.aborted and len(connector.calls) == 1
    if stage == "conversation.item.done":
        assert not any(e["type"] == "input_audio_buffer.append" for e in socket.sent)
    if stage == "input_audio_buffer.committed":
        assert not any(e["type"] == "response.create" for e in socket.sent)


def test_total_deadline_is_not_reset_by_slow_progress():
    request, wire = prepared()
    socket = Socket(delay=0.02)
    receipt = RealtimeTransport(timeout_seconds=0.05, connector=Connector(socket)).submit(
        request, wire, "key"
    )
    assert receipt.error == "TimeoutError" and not receipt.response_done
    assert "response.create" not in [event["type"] for event in socket.sent]


def test_connect_deadline_covers_handshake():
    request, wire = prepared()
    calls = []

    async def connector(*args, **kwargs):
        calls.append(args)
        await asyncio.Event().wait()

    receipt = RealtimeTransport(timeout_seconds=0.02, connector=connector).submit(request, wire, "key")
    assert receipt.handshake_status is None and receipt.error == "TimeoutError" and len(calls) == 1


def test_rejected_handshake_is_not_http_success_and_preserves_raw_body():
    request, wire = prepared()

    async def connector(*args, **kwargs):
        raise InvalidStatus(
            Response(401, "Unauthorized", Headers({"content-type": "application/json"}), b"denied")
        )

    receipt = RealtimeTransport(timeout_seconds=1, connector=connector).submit(request, wire, "key")
    assert receipt.handshake_status == 401 and receipt.complete and not receipt.response_done
    assert bytes.fromhex(json.loads(receipt.body)["events"][0]["raw"]) == b"denied"


def test_foreign_endpoint_rejected_before_auth_or_connect():
    request, wire = prepared()
    connector = Connector(Socket())
    request["url"] = "wss://other.example/v1/realtime"
    with pytest.raises(ContractError):
        RealtimeTransport(timeout_seconds=1, connector=connector).submit(request, wire, "secret")
    assert not connector.calls


def test_library_redirect_is_rejected_without_forwarding_authorization():
    async def scenario():
        target_calls, source_calls = [], []

        async def handler(socket):
            target_calls.append(socket.request)

        async with serve(handler, "127.0.0.1", 0) as target:
            target_port = target.sockets[0].getsockname()[1]

            def redirect(connection, request):
                source_calls.append(request)
                return Response(302, "Found", Headers({"Location": f"ws://127.0.0.1:{target_port}/"}), b"")

            async with serve(handler, "127.0.0.1", 0, process_request=redirect) as source:
                source_port = source.sockets[0].getsockname()[1]
                with pytest.raises(InvalidStatus):
                    await OriginBoundConnect(
                        f"ws://127.0.0.1:{source_port}/",
                        proxy=None,
                        additional_headers={"Authorization": "Bearer source-only"},
                    )
        assert len(source_calls) == 1 and not target_calls
        assert source_calls[0].headers["Authorization"] == "Bearer source-only"

    asyncio.run(scenario())
