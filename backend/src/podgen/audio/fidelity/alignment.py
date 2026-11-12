from difflib import SequenceMatcher

from .integers import units

VERSION = "literal-ru-integer-surface-equivalence/1"


def align(expected, actual):
    left, right = units(expected), units(actual)
    opcodes = []
    equivalences = []

    def boundary(items, index, total):
        return items[index]["start"] if index < len(items) else total

    for tag, a, b, c, d in SequenceMatcher(
        a=[x["key"] for x in left], b=[x["key"] for x in right], autojunk=False
    ).get_opcodes():
        if tag != "equal":
            opcodes.append(
                (
                    tag,
                    boundary(left, a, len(expected)),
                    boundary(left, b, len(expected)),
                    boundary(right, c, len(actual)),
                    boundary(right, d, len(actual)),
                )
            )
            continue
        for left_unit, right_unit in zip(left[a:b], right[c:d], strict=True):
            x, y = (
                expected[left_unit["start"] : left_unit["end"]],
                actual[right_unit["start"] : right_unit["end"]],
            )
            literal = [t["normalized"] for t in x] == [t["normalized"] for t in y]
            numeric = left_unit["value"] is not None and {left_unit["form"], right_unit["form"]} == {
                "digits",
                "words",
            }
            kind = "equal" if literal else "equivalent" if numeric else "replace"
            opcodes.append(
                (kind, left_unit["start"], left_unit["end"], right_unit["start"], right_unit["end"])
            )
            if kind == "equivalent":
                equivalences.append(
                    dict(
                        rule=VERSION,
                        value=left_unit["value"],
                        expected_token_range=[left_unit["start"], left_unit["end"]],
                        recognized_token_range=[right_unit["start"], right_unit["end"]],
                        expected_tokens=x,
                        recognized_tokens=y,
                    )
                )
    return opcodes, equivalences
