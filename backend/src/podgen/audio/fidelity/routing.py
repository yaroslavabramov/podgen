"""Who handles each script discrepancy: automatic repair, the user, or nobody.

- automatic_repair: the recording adds or omits at least three words of content.
- recognition_variant: the recognizer wrote the same words differently, either in
  another inflection of the same lemma (pymorphy3) or a Cyrillic name in Latin script.
- user_confirmation: everything else; a person decides from the clip.
"""

import re

import pymorphy3

RULE = "fidelity-review-routing/1"
CONTENT_WORDS = 3
CYRILLIC, LATIN = re.compile(r"^[а-яё]+$"), re.compile(r"^[a-z]+$")


def route(candidate, morph):
    if candidate["kind"] != "script_discrepancy":
        return "user_confirmation"
    expected = [token["normalized"] for token in candidate["expected_tokens"]]
    heard = [token["normalized"] for token in candidate["recognized_tokens"]]
    if abs(len(heard) - len(expected)) >= CONTENT_WORDS:
        return "automatic_repair"
    if expected and heard and all(CYRILLIC.match(w) for w in expected) and all(LATIN.match(w) for w in heard):
        return "recognition_variant"
    if (
        expected
        and len(expected) == len(heard)
        and all(
            {p.normal_form for p in morph.parse(a)} & {p.normal_form for p in morph.parse(b)}
            for a, b in zip(expected, heard, strict=True)
        )
    ):
        return "recognition_variant"
    return "user_confirmation"


def assign(candidates):
    morph = pymorphy3.MorphAnalyzer(lang="ru")
    for candidate in candidates:
        candidate["review_route"] = route(candidate, morph)
