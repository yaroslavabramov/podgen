"""Bounded tokenization windows over exact document coordinates, with resumable batches."""

from .chunks import spans


def batches(text, network, profile, *, cursor=0):
    for ranges, following, _ in prepared_batches(text, network, profile, cursor=cursor):
        yield ranges, following


def prepared_batches(text, network, profile, *, cursor=0):
    """Return the already read window's exact passages with their coordinates."""
    width = profile["tokenization_characters"]
    batch_size = profile["index_batch_size"]
    while cursor < len(text):
        overlap = profile["tokenization_overlap_characters"]
        stride = width - overlap
        anchor = (cursor // stride) * stride
        origin = max(0, anchor - overlap)
        stop = min(len(text), anchor + stride)
        window = text[origin:stop]
        ranges = [
            (origin + a, origin + b)
            for a, b in spans(
                window, network.offsets(window), profile["chunk_tokens"], profile["overlap_tokens"]
            )
        ]
        ranges = [pair for pair in ranges if pair[1] > cursor]
        # The tokenizer may omit whitespace. Cursor advancement still covers the
        # full original window, including all whitespace-only windows.
        for offset in range(0, len(ranges), batch_size):
            selected = ranges[offset : offset + batch_size]
            following = stop if offset + batch_size >= len(ranges) else selected[-1][1]
            yield selected, following, [window[a - origin : b - origin] for a, b in selected]
        if not ranges:
            yield [], stop, []
        cursor = stop
