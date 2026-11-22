"""Rebind explanation references only through authenticated native-round evidence.

Evidence text, script anchors, claim wording and quoted passages are never edited.
All mappings come from the full native report and its authenticated evidence table.
"""

import re

MENTION = re.compile(r"(?<!\w)[EBH][1-9][0-9]*(?!\w)")
QUOTED = re.compile(
    r'```.*?(?:```|\Z)|«[^»]*(?:»|\Z)|“[^”]*(?:”|\Z)|‘[^’]*(?:’|\Z)|„[^“]*(?:“|\Z)|"(?:\\.|[^"\\])*(?:"|\Z)|(?<!\w)\x27[^\x27\n]*\x27|^\s*>[^\n]*',
    re.DOTALL | re.MULTILINE,
)
FIELDS = ("contextual_meaning", "context_scope", "rationale", "memory_summary")


def rebind(text, aliases):
    def replace(part):
        return MENTION.sub(lambda match: aliases.get(match[0], match[0]), part)

    result, start = [], 0
    for quote in QUOTED.finditer(text):
        result.extend((replace(text[start : quote.start()]), quote[0]))
        start = quote.end()
    result.append(replace(text[start:]))
    return "".join(result)


def alias_number(name):
    return int(name[1:]) if re.fullmatch(r"H[1-9][0-9]*", name) else 0


def text_aliases(catalog, aliases):
    candidates = {}
    for key in catalog:
        if key in aliases:
            candidates.setdefault(key.partition(":")[2], set()).add(aliases[key]["evidence_id"])
    # Bare names cannot choose between conflicting current/previous references.
    return {name: next(iter(values)) for name, values in candidates.items() if len(values) == 1}


def fields(row, replacements):
    return dict(row, **{key: rebind(row[key], replacements) for key in FIELDS})
