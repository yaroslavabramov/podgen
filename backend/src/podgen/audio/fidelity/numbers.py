"""Digits that the recognizer wrote for a number the script spells out, and the reverse.

The spoken forms come from num2words: every cardinal and ordinal form of the value in
all six cases, both numbers, three genders and animacy. A hunk is equivalent only when
one side is a single plain digits token and the other side is exactly one of those forms,
written as words of one utterance separated by spaces.
"""

from num2words import num2words

RULE = "num2words-ru-integer-forms/1"
CASES = ("nominative", "genitive", "dative", "accusative", "instrumental", "prepositional")
MAX_DIGITS = 9


def forms(value):
    result = set()
    for kind in ("cardinal", "ordinal"):
        for case in CASES:
            for plural in (False, True):
                for gender in ("m", "f", "n"):
                    for animate in (True, False):
                        text = num2words(
                            value,
                            lang="ru",
                            to=kind,
                            case=case,
                            plural=plural,
                            gender=gender,
                            animate=animate,
                        )
                        words = tuple(text.casefold().replace("ё", "е").split())
                        # num2words also applies the gender to a count of millions ("две миллиона").
                        if any(
                            word in ("одна", "одно", "две") and following.startswith("миллион")
                            for word, following in zip(words, words[1:], strict=False)
                        ):
                            continue
                        result.add(words)
                        # "одна тысяча восемьсот" is also said as "тысяча восемьсот".
                        if (
                            len(words) > 1
                            and words[0] in ("один", "одна")
                            and words[1][:5] in ("тысяч", "милли")
                        ):
                            result.add(words[1:])
    return result


def integer(tokens):
    if len(tokens) == 1:
        word = tokens[0]["normalized"]
        if word.isascii() and word.isdecimal() and len(word) <= MAX_DIGITS and word == str(int(word)):
            return int(word)
    return None


def spoken(tokens):
    if not tokens or not all(token["normalized"].isalpha() for token in tokens):
        return None
    if any(token.get("utterance_index") != tokens[0].get("utterance_index") for token in tokens):
        return None
    if any(
        token.get("separator_before", " ").strip() or "\n" in token.get("separator_before", " ")
        for token in tokens[1:]
    ):
        return None
    return tuple(token["normalized"] for token in tokens)


def equivalent(expected, recognized, known):
    """The shared value, or None. `known` holds the forms computed within this alignment."""
    for digits, words in ((recognized, expected), (expected, recognized)):
        value, said = integer(digits), spoken(words)
        if value is None or said is None:
            continue
        if value not in known:
            known[value] = forms(value)
        if said in known[value]:
            return value
    return None
