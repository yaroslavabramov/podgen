"""HTTP completion and WebSocket response completion have different evidence."""

from podgen.audio.realtime_transport import RealtimeReceipt


def fields(response):
    if isinstance(response, RealtimeReceipt):
        return dict(
            contract="audio-review-websocket-receipt/1",
            transport="websocket",
            handshake_status=response.handshake_status,
            handshake_headers=response.handshake_headers,
            response_done=response.response_done,
        )
    return dict(
        contract="audio-review-http-receipt/1", status_code=response.status_code, headers=response.headers
    )


def media_type(response):
    return (
        "application/json"
        if isinstance(response, RealtimeReceipt)
        else response.headers.get("content-type", "application/octet-stream")
    )


def processable(receipt):
    if not receipt or not receipt["complete"] or receipt["error"]:
        return False
    if receipt.get("transport") == "websocket":
        return receipt["handshake_status"] == 101 and receipt["response_done"]
    return 200 <= receipt["status_code"] < 300
