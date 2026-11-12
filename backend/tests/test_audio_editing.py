import pytest

from podgen.audio.assembly.pcm import read
from podgen.audio.editing import timeline
from podgen.audio.recognition.contracts import Recognition
from tests.audio_cases import signal


def inputs():
    text = "Первый текст. Второй текст. Третий текст."
    words = text.replace(".", "").split()
    recognized = Recognition(
        text=text,
        duration=9,
        request_id="fixture",
        segments=[
            dict(text=word, start=start, end=start + 0.4)
            for word, start in zip(words, [0.2, 0.7, 3.2, 3.7, 6.2, 6.7], strict=True)
        ],
    )
    return (
        [dict(utterance_id="fixture", speaker="HOST", text=text)],
        recognized,
        read(signal(rate=48000, seconds=9)),
    )


def test_long_equal_opcode_is_cropped_to_sentence_without_invented_word_times(monkeypatch):
    utterances, recognized, (params, frames, pcm) = inputs()
    monkeypatch.setattr(timeline, "align", lambda *args: ([("equal", 0, 6, 0, 6)], []))
    value = timeline.build(utterances, recognized, pcm, params[1], params[2], frames)
    assert len(value["units"]) == 3
    assert [u["first_word_frame"] for u in value["units"]] == [9600, 153600, 297600]
    assert value["units"][1]["script_ranges"][0]["text"] == "Второй текст."
    assert all(
        a["end_frame"] == b["start_frame"] for a, b in zip(value["units"], value["units"][1:], strict=False)
    )


@pytest.mark.parametrize("tag", ["replace", "equivalent"])
def test_ambiguous_alignment_crossing_sentence_boundary_is_whole_group(monkeypatch, tag):
    utterances, recognized, (params, frames, pcm) = inputs()
    monkeypatch.setattr(
        timeline,
        "align",
        lambda *args: ([("equal", 0, 1, 0, 1), (tag, 1, 3, 1, 3), ("equal", 3, 6, 3, 6)], []),
    )
    value = timeline.build(utterances, recognized, pcm, params[1], params[2], frames)
    assert len(value["units"]) == 2
    assert [s["text"] for s in value["units"][0]["script_ranges"]] == ["Первый текст.", "Второй текст."]
    assert value["units"][0]["last_word_frame"] == 196800
