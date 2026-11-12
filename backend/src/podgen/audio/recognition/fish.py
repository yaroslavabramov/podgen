import hashlib
import json

from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

from .contracts import VERSION, Recognition


def prepare(profile, audio):
    fields = dict(
        language=profile.language, ignore_timestamps="false", diarize="false", tag_audio_events="false"
    )
    boundary = "podgen-asr-" + hashlib.sha256(audio + canonical_bytes(fields)).hexdigest()
    if boundary.encode() in audio:
        raise ContractError("recognition_multipart_boundary_collision")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio.flac"\r\nContent-Type: audio/flac\r\n\r\n'.encode()
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    wire = b"".join(parts)
    return dict(
        adapter_version=VERSION,
        method="POST",
        url="https://api.fish.audio/v1/asr",
        headers={
            "model": profile.model_id,
            "content-type": "multipart/form-data; boundary=" + boundary,
            "accept": "application/json",
            "accept-encoding": "identity",
        },
        fields=fields,
        audio_sha256=hashlib.sha256(audio).hexdigest(),
        wire_sha256=hashlib.sha256(wire).hexdigest(),
        unprompted=True,
    ), wire


def parse(raw):
    value = json.loads(raw)
    return Recognition.model_validate({k: value[k] for k in ("text", "duration", "segments", "request_id")})
