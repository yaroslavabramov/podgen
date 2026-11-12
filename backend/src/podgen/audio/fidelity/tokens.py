import re

VERSION = "literal-ru-case-punctuation-yo/1"
# Decimal points, numeric signs, ratios and date separators remain meaningful.
WORDS = re.compile(
    r"(?<!\w)[+−-]\d+(?:[.,:/-]\d+)*|\d+(?:[.,:/-]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE
)


def tokens(text, **origin):
    return [
        dict(
            index=i,
            text=m.group(),
            normalized=m.group().casefold().replace("ё", "е"),
            char_start=m.start(),
            char_end=m.end(),
            **origin,
        )
        for i, m in enumerate(WORDS.finditer(text))
    ]


def expected_tokens(utterances):
    result = []
    for i, row in enumerate(utterances):
        for token in tokens(row["text"], utterance_index=i, utterance_id=row["utterance_id"]):
            result.append(dict(token, index=len(result)))
    return result
