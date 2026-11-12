"""Choose an exact frame inside a recognizer's inter-word gap, never a proportional word time."""

import numpy as np


def samples(pcm, width):
    if width in (2, 4):
        return np.frombuffer(pcm, dtype="<i2" if width == 2 else "<i4").astype(np.float64)
    data = np.frombuffer(pcm, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    values = data[:, 0] | data[:, 1] << 8 | data[:, 2] << 16
    return ((values ^ 0x800000) - 0x800000).astype(np.float64)


def gap_frame(pcm, width, rate, left_end, right_start, frames):
    first, last = max(0, left_end), min(frames, right_start)
    if first >= last:
        return max(0, min(frames, right_start)), "recognizer_word_boundary_no_verified_pause"
    data = samples(pcm[first * width : last * width], width)
    block = max(1, min(len(data), round(rate * 0.01)))
    # Fixed-length non-overlapping blocks keep selection deterministic and bounded.
    starts = list(range(0, len(data) - block + 1, block))
    offset = min(starts, key=lambda i: float(np.mean(np.square(data[i : i + block]))))
    midpoint = offset + block // 2
    nearby = range(
        max(offset, midpoint - max(1, rate // 500)), min(len(data), midpoint + max(1, rate // 500) + 1)
    )
    point = min(nearby, key=lambda i: (abs(data[i]), abs(i - midpoint)))
    return first + point, "minimum_energy_in_recognizer_word_gap"
