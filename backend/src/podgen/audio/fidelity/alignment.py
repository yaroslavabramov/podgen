"""Kaldi edit alignment over reversible raw-token comparison units."""

from importlib.metadata import version

import kaldialign

from podgen.domain.errors import ContractError

from . import numbers

VERSION = "kaldialign-text-alignment/1"
INTEGER_RULE = numbers.RULE
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


def label(token):
    if token["normalized"].isalpha():
        return token["normalized"]
    # Delimited nonalphabetic tokens cannot concatenate into another single token.
    return "\x01" + token["normalized"].encode("utf-8").hex() + "\x02"


def consume(text, labels, offset):
    if text == EPSILON:
        return offset
    parts = text.split(" ")
    end = offset + len(parts)
    if labels[offset:end] != parts:
        raise ContractError("fidelity_alignment_lost_unit_offsets")
    return end


def align(expected, actual):
    left_labels = [label(token) for token in expected]
    right_labels = [label(token) for token in actual]
    opcodes, equivalences = [], []
    a = c = 0
    for lhs, rhs in kaldialign.align(
        left_labels, right_labels, EPSILON, sclite_mode=False, merge_compounds=True
    ):
        b, d = consume(lhs, left_labels, a), consume(rhs, right_labels, c)
        x, y = expected[a:b], actual[c:d]
        literal = [t["normalized"] for t in x] == [t["normalized"] for t in y]
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
            if compound
            else "replace"
        )
        i1, i2, j1, j2 = a, b, c, d
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
            equivalences.append(
                dict(
                    rule=COMPOUND_RULE,
                    expected_token_range=[i1, i2],
                    recognized_token_range=[j1, j2],
                    expected_tokens=x,
                    recognized_tokens=y,
                    joined_form=lhs.replace(" ", ""),
                )
            )
        a, c = b, d
    if a != len(expected) or c != len(actual):
        raise ContractError("fidelity_alignment_incomplete_unit_coverage")
    # A whole mismatch hunk that is a number written once as digits and once as words.
    known = {}
    for index, (kind, i1, i2, j1, j2) in enumerate(opcodes):
        if kind != "replace":
            continue
        value = numbers.equivalent(expected[i1:i2], actual[j1:j2], known)
        if value is not None:
            opcodes[index] = ("equivalent", i1, i2, j1, j2)
            equivalences.append(
                dict(
                    rule=INTEGER_RULE,
                    expected_token_range=[i1, i2],
                    recognized_token_range=[j1, j2],
                    expected_tokens=expected[i1:i2],
                    recognized_tokens=actual[j1:j2],
                    value=value,
                )
            )
    equivalences.sort(key=lambda item: item["expected_token_range"])
    return opcodes, equivalences
