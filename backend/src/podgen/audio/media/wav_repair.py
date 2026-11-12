"""Explicit container-only copy for the observed Fish streaming placeholder header."""

import hashlib
import struct

from podgen.domain.errors import ContractError


def repair_fish_stream(data, *, complete, status_code, sample_rate_hz):
    if not complete or status_code != 200:
        raise ContractError("wav_repair_requires_complete_http_200")
    if len(data) < 46:
        raise ContractError("wav_repair_invalid_header")
    expected = (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFF24)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate_hz, sample_rate_hz * 2, 2, 16)
        + b"data"
        + struct.pack("<I", 0xFFFFFF00)
    )
    if data[:44] != expected or (len(data) - 44) % 2 or len(data) >= 0xFFFFFF00:
        raise ContractError("wav_repair_not_observed_fish_pcm16_placeholder")
    result = bytearray(data)
    patches = []
    for offset, value in ((4, len(data) - 8), (40, len(data) - 44)):
        patches.append(dict(offset=offset, old=struct.unpack_from("<I", data, offset)[0], new=value))
        struct.pack_into("<I", result, offset, value)
    repaired = bytes(result)
    return repaired, dict(
        contract="fish-observed-stream-container-repair/1",
        original_sha256=hashlib.sha256(data).hexdigest(),
        repaired_sha256=hashlib.sha256(repaired).hexdigest(),
        pcm_payload_sha256=hashlib.sha256(data[44:]).hexdigest(),
        payload_unchanged=repaired[44:] == data[44:],
        sample_frames=(len(data) - 44) // 2,
        patches=patches,
        speech_completeness="pending_listening",
        basis="explicit local repair of observed placeholder lengths; not server completion proof",
    )
