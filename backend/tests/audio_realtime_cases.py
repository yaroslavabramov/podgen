"""Protocol doubles keep acknowledgement order observable; no acoustic claims."""

import asyncio
import json
from collections import deque
from types import SimpleNamespace

from podgen.audio.fidelity.adjudication.realtime import TEXT_ITEM_ID
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfigV3


def configuration(old, **overrides):
    raw = old.model_dump(mode="json")
    raw.update(
        contract="audio-fidelity-adjudication/3",
        zero_pcm_policy="inconclusive-audio-unavailable/1",
        **overrides,
    )
    raw["fallback"]["reviewer"] = dict(model_id="gpt-realtime-2.1")
    raw["fallback"]["price"].update(
        usd_per_million_input_text_tokens="4", usd_per_million_output_text_tokens="24"
    )
    return ReviewConfigV3.model_validate(raw)


def done(payload):
    results = [
        dict(
            case_id=c["case_id"],
            status="dismissed",
            reason_code="asr_error",
            heard_spans=[dict(start_seconds=0, end_seconds=0.1, words="слова")],
            explanation="Mechanical fixture.",
        )
        for c in payload["cases"]
    ]
    return dict(
        type="response.done",
        response=dict(
            id="realtime-fixture",
            status="completed",
            output=[
                dict(
                    type="message", status="completed", content=[dict(type="output_text", text="Commentary")]
                ),
                dict(
                    type="function_call",
                    name="submit_audio_review",
                    status="completed",
                    arguments=json.dumps(dict(results=results)),
                ),
            ],
            usage=dict(
                input_tokens=1047,
                output_tokens=103,
                input_token_details=dict(text_tokens=927, audio_tokens=120),
                output_token_details=dict(text_tokens=103, audio_tokens=0),
            ),
        ),
    )


class Socket:
    def __init__(self, *, delay=0, stall=None):
        self.queue = deque([dict(type="session.created")])
        self.sent, self.received = [], []
        self.delay, self.stall, self.aborted = delay, stall, False
        self.response = SimpleNamespace(status_code=101, headers={"upgrade": "websocket"})
        self.transport = SimpleNamespace(abort=self.abort)
        self.payload = None

    def abort(self):
        self.aborted = True

    async def recv(self):
        await asyncio.sleep(self.delay)
        if not self.queue:
            await asyncio.Event().wait()
        event = self.queue.popleft()
        if event["type"] == self.stall:
            await asyncio.Event().wait()
        self.received.append(event["type"])
        return json.dumps(event, ensure_ascii=False)

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        kind = event["type"]
        if kind == "session.update":
            assert "session.created" in self.received
            self.queue.append(dict(type="session.updated"))
        elif kind == "conversation.item.create":
            assert "session.updated" in self.received
            self.payload = json.loads(event["item"]["content"][0]["text"])
            self.queue.append(dict(type="conversation.item.done", item=dict(id=TEXT_ITEM_ID)))
        elif kind == "input_audio_buffer.append":
            assert "conversation.item.done" in self.received
        elif kind == "input_audio_buffer.commit":
            assert self.sent[-2]["type"] == "input_audio_buffer.append"
            self.queue.append(dict(type="input_audio_buffer.committed"))
        elif kind == "response.create":
            assert "input_audio_buffer.committed" in self.received
            self.queue.append(done(self.payload))


class Connector:
    def __init__(self, socket):
        self.socket, self.calls = socket, []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.socket
