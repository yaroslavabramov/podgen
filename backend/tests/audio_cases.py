"""Signal and transport fixtures test mechanics only; they do not simulate a voice qualification."""

import base64
import hashlib
import io
import json
import wave
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4, uuid5

import numpy as np

from podgen.audio.synthesis.contracts import (
    MODELS,
    HttpReceipt,
    SynthesisInput,
    SynthesisProfile,
    SynthesisUtterance,
)
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.audio.contracts import ComparisonConfig


def signal(rate=24000, seconds=5, extremes=False):
    t = np.arange(rate * seconds) / rate
    data = (
        4000
        * (np.sin(2 * np.pi * 150 * t) + 0.3 * np.sin(2 * np.pi * 430 * t))
        * (0.7 + 0.3 * np.sin(2 * np.pi * 2 * t))
    ).astype("<i2")
    if extremes:
        data[:2] = [32767, -32768]
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        f.writeframes(data.tobytes())
    return out.getvalue()


def source(text="Точное русское произношение. Без лишних слов."):
    return SynthesisInput(utterances=[SynthesisUtterance(utterance_id=uuid4(), speaker="HOST", text=text)])


def profile(provider="elevenlabs", **changes):
    return SynthesisProfile(
        provider=provider, model_id=MODELS[provider], voice_id="fixture-" + provider, **changes
    )


def response(provider, audio):
    encoded = base64.b64encode(audio).decode()
    envelopes = dict(
        elevenlabs=dict(audio_base64=encoded, alignment=dict(characters=["Т"])),
        gemini=dict(
            model=MODELS["gemini"],
            status="completed",
            id="gemini-receipt",
            steps=[
                dict(type="model_output", content=[dict(type="audio", mime_type="audio/wav", data=encoded)])
            ],
            usage=dict(total_input_tokens=10, output_tokens_by_modality=[dict(modality="audio", tokens=100)]),
        ),
        inworld=dict(
            audioContent=encoded, usage=dict(modelId=MODELS["inworld"], processedCharactersCount=20)
        ),
        minimax=dict(
            data=dict(audio=audio.hex(), status=2),
            extra_info=dict(
                audio_sample_rate=24000, audio_channel=1, audio_format="wav", usage_characters=20
            ),
            base_resp=dict(status_code=0),
            trace_id="minimax-receipt",
        ),
    )
    raw = audio if provider == "fish" else json.dumps(envelopes[provider]).encode()
    return HttpReceipt(
        200,
        {
            "content-type": "audio/wav" if provider == "fish" else "application/json",
            "request-id": "fixture-request",
            "character-cost": "123",
        },
        raw,
        50,
    )


class MemoryJournal:
    def __init__(self):
        self.run_id = uuid4()
        self.records = {}
        self.blobs = {}
        self.repo = SimpleNamespace(actor_id=uuid4())
        self.base = self

    def ref(self, key):
        return RuntimeRef(record_kind="tool_execution", record_id=uuid5(self.run_id, key))

    def get(self, key):
        return deepcopy(self.records.get(str(self.ref(key).record_id)))

    def put(self, key, value):
        record = str(self.ref(key).record_id)
        if record in self.records:
            assert self.records[record] == value
        self.records[record] = deepcopy(value)
        return self.ref(key)

    def blob(self, body, media):
        key = hashlib.sha256(body + media.encode()).hexdigest()
        self.blobs[key] = body
        return dict(
            storage_key=key,
            sha256=hashlib.sha256(body).hexdigest(),
            byte_length=len(body),
            media_type=media,
        )

    def bytes(self, key, blob):
        return self.blobs[blob["storage_key"]]

    def read_blob(self, ref, blob):
        return self.blobs[blob.storage_key]

    def read(self, ref):
        return deepcopy(self.records[str(ref.record_id)])

    def record(self, value):
        return self.put(str(uuid4()), value)

    def lock(self):
        return nullcontext()


def config(providers=None):
    return ComparisonConfig.model_validate(
        dict(
            run_id=str(uuid4()),
            providers=providers or ["elevenlabs"],
            profiles=[
                dict(
                    profile_id="voice-a",
                    synthesis=profile().model_dump(mode="json"),
                    credential_env="TEST_SYNTHESIS_KEY",
                    rates=[
                        dict(
                            unit="characters",
                            usd_per_million="80",
                            source_url="https://elevenlabs.io/pricing/api",
                            checked_at="2026-11-12",
                        )
                    ],
                    reservation_usd="0.50",
                )
            ],
            fragments=[
                dict(
                    fragment_id=f"fragment-{i}",
                    outcome_ref=RuntimeRef(record_kind="tool_execution", record_id=uuid4()).model_dump(
                        mode="json"
                    ),
                    selections=[dict(utterance_index=i)],
                )
                for i in (0, 1)
            ],
            max_cost_usd="2",
            max_attempts=4,
            blind_seed=42,
        )
    )
