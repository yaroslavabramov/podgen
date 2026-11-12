import hashlib
import io
import wave

from podgen.domain.errors import ContractError


def read(audio):
    try:
        with wave.open(io.BytesIO(audio), "rb") as stream:
            params = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate())
            frames = stream.getnframes()
            pcm = stream.readframes(frames)
            if (
                params[0] != 1
                or params[1] not in (2, 3, 4)
                or not frames
                or len(pcm) != frames * params[0] * params[1]
            ):
                raise ContractError("assembly_invalid_pcm_frames")
            return params, frames, pcm
    except (wave.Error, EOFError) as error:
        raise ContractError("assembly_invalid_pcm_container") from error


def concatenate(inputs):
    if not inputs:
        raise ContractError("assembly_requires_audio_parts")
    first = None
    payload = []
    mapping = []
    offset = 0
    for audio in inputs:
        params, frames, pcm = read(audio)
        if first is None:
            first = params
        if params != first:
            raise ContractError("assembly_parts_pcm_format_mismatch")
        mapping.append(
            dict(
                source_start_frame=0,
                source_end_frame=frames,
                master_start_frame=offset,
                master_end_frame=offset + frames,
                source_audio_sha256=hashlib.sha256(audio).hexdigest(),
                source_pcm_sha256=hashlib.sha256(pcm).hexdigest(),
            )
        )
        payload.append(pcm)
        offset += frames
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setparams((*first, 0, "NONE", "not compressed"))
        for pcm in payload:
            output.writeframesraw(pcm)
    data = target.getvalue()
    _, actual_frames, actual = read(data)
    if actual_frames != offset or actual != b"".join(payload):
        raise ContractError("assembly_pcm_not_bit_identical")
    return data, dict(
        contract="exact-concatenated-pcm/1",
        sample_rate_hz=first[2],
        channels=first[0],
        sample_width_bytes=first[1],
        sample_frames=offset,
        pcm_sha256=hashlib.sha256(actual).hexdigest(),
        parts=mapping,
        operations=["concatenate_full_parts"],
        cuts=False,
        crossfades=False,
        inserted_silence=False,
        tempo_changed=False,
    )
