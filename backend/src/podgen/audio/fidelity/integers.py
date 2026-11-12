"""Conservative integer surface forms, not general semantic or pronunciation equivalence."""

import re

from .number_words import CASES, CONTEXT, DAY_ORDINALS, HUNDREDS, MONTHS, SCALES, TEENS, TENS, UNITS, WORDS

ORDINAL = re.compile(
    r"^(перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят|двадцат|тридцат|сороков|пятидесят|шестидесят|семидесят|восьмидесят|девяност|сот|двухсот|тысячн|миллионн)(ый|ой|ий|ая|ое|ого|ому|ом|ые|ых|ым|ыми|ую|ьего|ьем|ьему|ья|ье)$"
)


def under_thousand(words):
    if words:
        common = set.intersection(*(CASES[w] for w in words))
        if not common:
            return None
    value = 0
    if words and words[0] in HUNDREDS:
        value += HUNDREDS[words.pop(0)]
    if words and words[0] in TEENS:
        value += TEENS[words.pop(0)]
    else:
        if words and words[0] in TENS:
            value += TENS[words.pop(0)]
        if words and words[0] in UNITS:
            value += UNITS[words.pop(0)]
    return value if not words else None


def parse(words):
    if len(words) > 1 and "ноль" in words:
        return None
    total = 0
    previous_scale = 10**9
    start = 0
    for i, word in enumerate(words):
        if word not in SCALES:
            continue
        scale = SCALES[word]
        if scale >= previous_scale:
            return None
        if i == start and start > 0:
            return None
        group = under_thousand(list(words[start:i])) if i > start else 1
        if group is None or group == 0:
            return None
        last_word = words[i - 1] if i > start else None
        if scale == 1000 and last_word in {"один", "одно", "два", "одного"}:
            return None
        if scale == 1000000 and last_word in {"одна", "одно", "две", "одной"}:
            return None
        # Scale inflection must agree with the count; malformed forms stay uncertain.
        last, teen = group % 10, group % 100
        expected = (
            (
                "тысяча"
                if last == 1 and teen != 11
                else "тысячи"
                if last in (2, 3, 4) and teen not in (12, 13, 14)
                else "тысяч"
            )
            if scale == 1000
            else (
                "миллион"
                if last == 1 and teen != 11
                else "миллиона"
                if last in (2, 3, 4) and teen not in (12, 13, 14)
                else "миллионов"
            )
        )
        if i > start and CASES[words[start]] == {"oblique"}:
            expected = (
                ("тысячи" if group == 1 else "тысяч")
                if scale == 1000
                else ("миллиона" if group == 1 else "миллионов")
            )
        if word != expected:
            return None
        total += group * scale
        previous_scale = scale
        start = i + 1
    tail = under_thousand(list(words[start:]))
    return total + tail if tail is not None else None


def units(tokens):
    result = []
    i = 0
    while i < len(tokens):
        word = tokens[i]["normalized"]
        end = i + 1
        value = None
        form = None
        if word.isascii() and word.isdecimal() and len(word) <= 9 and word == str(int(word)):
            value = int(word)
            form = "digits"
        elif word in WORDS or word in DAY_ORDINALS:
            while (
                end < len(tokens)
                and tokens[end]["normalized"] in WORDS | set(DAY_ORDINALS)
                and tokens[end].get("utterance_index") == tokens[i].get("utterance_index")
                and tokens[end].get("separator_before", " ")
                and all(c in " \t" for c in tokens[end].get("separator_before", " "))
            ):
                end += 1
            values = [t["normalized"] for t in tokens[i:end]]
            nearby = {tokens[j]["normalized"] for j in (i - 1, end) if 0 <= j < len(tokens)}
            # Single words can be names/verbs; require an explicit numeric context.
            if values[-1] in DAY_ORDINALS:
                if (
                    end < len(tokens)
                    and tokens[end]["normalized"] in MONTHS
                    and tokens[end].get("separator_before", " ").strip() == ""
                    and "\n" not in tokens[end].get("separator_before", " ")
                ):
                    prefix_value = (
                        TENS[values[0]]
                        if len(values) == 2 and values[0] in ("двадцать", "тридцать")
                        else 0
                        if len(values) == 1
                        else None
                    )
                    if prefix_value in (0, 20, 30):
                        day = prefix_value + DAY_ORDINALS[values[-1]]
                        if 1 <= day <= 31:
                            value = day
            elif len(values) > 1 or nearby & CONTEXT:
                value = parse(values)
            next_word = tokens[end]["normalized"] if end < len(tokens) else ""
            if ORDINAL.fullmatch(next_word):
                value = None
            if next_word in {"целых", "целая", "целое", "десятых", "сотых", "тысячных"}:
                value = None
            form = "words"
        key = (
            f"integer:{value}"
            if value is not None
            else "literal:" + "|".join(t["normalized"] for t in tokens[i:end])
        )
        result.append(dict(key=key, start=i, end=end, value=value, form=form))
        i = end
    return result
