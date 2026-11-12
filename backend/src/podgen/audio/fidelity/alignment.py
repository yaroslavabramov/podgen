"""Kaldi edit alignment over reversible raw-token comparison units."""

from importlib.metadata import version

import kaldialign

from podgen.domain.errors import ContractError

from .integers import units

VERSION = "kaldialign-text-alignment/1"
INTEGER_RULE = "literal-ru-integer-surface-equivalence/1"
COMPOUND_RULE = "literal-alphabetic-compound-equivalence/1"
EPSILON = "\x00"


def provenance():
    return dict(
        contract=VERSION,
        engine="kaldialign",
        engine_version=version("kaldialign"),
        merge_compounds="alphabetic_literal_units_only",
        sclite_mode=False,
        integer_rule=INTEGER_RULE,
        compound_rule=COMPOUND_RULE,
    )


def label(unit, tokens):
    raw = tokens[unit["start"] : unit["end"]]
    if unit["value"] is None and len(raw) == 1 and raw[0]["normalized"].isalpha():
        return raw[0]["normalized"]
    # Delimited nonalphabetic units cannot concatenate into another single unit.
    return "\x01" + unit["key"].encode("utf-8").hex() + "\x02"


def consume(text, labels, offset):
    if text == EPSILON:
        return offset
    parts = text.split(" ")
    end = offset + len(parts)
    if labels[offset:end] != parts:
        raise ContractError("fidelity_alignment_lost_unit_offsets")
    return end


def align(expected, actual):
    left, right = units(expected), units(actual)
    left_labels = [label(unit, expected) for unit in left]
    right_labels = [label(unit, actual) for unit in right]
    opcodes, equivalences = [], []
    a = c = 0

    def boundary(items, index, total):
        return items[index]["start"] if index < len(items) else total

    for lhs, rhs in kaldialign.align(
        left_labels, right_labels, EPSILON, sclite_mode=False, merge_compounds=True
    ):
        b, d = consume(lhs, left_labels, a), consume(rhs, right_labels, c)
        i1, i2 = boundary(left, a, len(expected)), boundary(left, b, len(expected))
        j1, j2 = boundary(right, c, len(actual)), boundary(right, d, len(actual))
        x, y = expected[i1:i2], actual[j1:j2]
        literal = [t["normalized"] for t in x] == [t["normalized"] for t in y]
        numeric = (
            b - a == d - c == 1
            and left[a]["value"] is not None
            and left[a]["key"] == right[c]["key"]
            and {left[a]["form"], right[c]["form"]} == {"digits", "words"}
        )
        compound = (
            bool(x and y)
            and (b - a > 1 or d - c > 1)
            and all(word.isalpha() for word in lhs.split(" ") + rhs.split(" "))
            and lhs.replace(" ", "") == rhs.replace(" ", "")
        )
        kind = (
            "insert"
            if not x
            else "delete"
            if not y
            else "equal"
            if literal
            else "equivalent"
            if numeric or compound
            else "replace"
        )
        # Preserve contiguous mismatch hunks for downstream source context/localization.
        if (
            kind in ("insert", "delete", "replace")
            and opcodes
            and opcodes[-1][0] not in ("equal", "equivalent")
        ):
            _, p, _, q, _ = opcodes.pop()
            kind = "insert" if p == i2 else "delete" if q == j2 else "replace"
            i1, j1 = p, q
        opcodes.append((kind, i1, i2, j1, j2))
        if kind == "equivalent":
            item = dict(
                rule=INTEGER_RULE if numeric else COMPOUND_RULE,
                expected_token_range=[i1, i2],
                recognized_token_range=[j1, j2],
                expected_tokens=x,
                recognized_tokens=y,
            )
            if numeric:
                item["value"] = left[a]["value"]
            else:
                item["joined_form"] = lhs.replace(" ", "")
            equivalences.append(item)
        a, c = b, d
    if a != len(left) or c != len(right):
        raise ContractError("fidelity_alignment_incomplete_unit_coverage")
    return opcodes, equivalences
