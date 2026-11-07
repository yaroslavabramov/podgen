"""Punctuation-delimited source context, independent of language and historical selector IDs."""

import re

_BOUNDARY = re.compile(r'(?<=[。！？])(?:[」』”"]*)|(?<=[.!?…])(?:[»”")]+)?(?=\s|$)|\n\s*\n')


def source_contexts(text):
    start, result = 0, []
    for boundary in _BOUNDARY.finditer(text):
        end = boundary.end()
        if start < end:
            result.append((start, end))
        start = end
        while start < len(text) and text[start].isspace():
            start += 1
    if result and start < len(text):
        result.append((start, len(text)))
    return result
