import re
import unicodedata

VERSION = "literal-ru-case-punctuation-yo/2"
WORDS = re.compile(
    r"(?<!\w)[+−-]\d+(?:[.,:/-]\d+)*|\d+(?:[.,:/-]\d+)*|[^\W\d_]+(?:['’][^\W\d_]+)*|[^\w\s]", re.UNICODE
)


def tokens(text, **origin):
    result = []
    for match in WORDS.finditer(text):
        word = match.group()
        # Punctuation is ignorable; mathematical/currency/unit symbols carry meaning.
        if len(word) == 1 and unicodedata.category(word).startswith("P") and word not in "%‰‱":
            tail = text[match.end() :]
            spaced_negative = (
                word == "-"
                and tail[:1].isspace()
                and tail.lstrip()[:1].isdigit()
                and (match.start() == 0 or not text[match.start() - 1].isalnum())
            )
            if not spaced_negative:
                continue
        result.append(
            dict(
                index=len(result),
                text=word,
                normalized=word.casefold().replace("ё", "е"),
                char_start=match.start(),
                char_end=match.end(),
                **origin,
            )
        )
    return result


def expected_tokens(utterances):
    result = []
    for i, row in enumerate(utterances):
        for token in tokens(row["text"], utterance_index=i, utterance_id=row["utterance_id"]):
            result.append(dict(token, index=len(result)))
    return result
