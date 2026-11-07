"""Bind source separators explicitly; never strip or rewrite arbitrary replacement text."""


def visible_range(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while start < end and text[end - 1].isspace():
        end -= 1
    if start == end:
        raise ValueError("revision_target_has_no_visible_text")
    return start, end


def deletion_buffer(text, start, end):
    left, right = start, end
    while left > 0 and text[left - 1].isspace():
        left -= 1
    while right < len(text) and text[right].isspace():
        right += 1
    if left == 0 and right == len(text):
        first, last = 0, len(text)
    elif left == 0:
        first, last = 0, right
    elif right == len(text):
        first, last = left, right
    elif left < start and right > end:
        first, last = start, right
    else:
        first, last = start, end
    return dict(start=first, end=last, text=text[first:last])


def replacement_separator_check(text, start, end, replacement):
    if (
        start > 0
        and text[start - 1].isspace()
        and replacement[0].isspace()
        or end < len(text)
        and text[end].isspace()
        and replacement[-1].isspace()
    ):
        raise ValueError("revision_duplicate_boundary_separator")


def normalize_edits(text, edits):
    normalized = []
    for start, end, replacement in sorted(edits):
        if normalized and start < normalized[-1][1]:
            previous = normalized[-1]
            if replacement == previous[2] == "" and text[start : previous[1]].isspace():
                normalized[-1] = (previous[0], max(end, previous[1]), "")
                continue
            raise ValueError("revision_text_edits_overlap")
        normalized.append((start, end, replacement))
    return normalized
