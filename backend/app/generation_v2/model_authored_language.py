from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
import unicodedata
from typing import Any


_SCRIPT_BY_LANGUAGE = {
    "ru": "CYRILLIC",
    "uk": "CYRILLIC",
    "be": "CYRILLIC",
    "bg": "CYRILLIC",
    "mk": "CYRILLIC",
    "kk": "CYRILLIC",
    "ky": "CYRILLIC",
    "tg": "CYRILLIC",
    "el": "GREEK",
    "ar": "ARABIC",
    "fa": "ARABIC",
    "he": "HEBREW",
    "hy": "ARMENIAN",
    "ka": "GEORGIAN",
}
_MIN_FIELD_ALPHA_CHARS = 48
_MIN_TARGET_SCRIPT_CHARS = 2
_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", re.UNICODE)
_MIN_FOREIGN_LOWERCASE_WORD_CHARS = 4
_MIN_FOREIGN_PROSE_WORDS = 2


def model_authored_language_error(
    *,
    output_language: str,
    fields: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Reject prose in the wrong script or a clearly code-switched foreign clause.

    Latin-script languages are deliberately not guessed. Exact source text is
    not passed to this helper, so quotations and evidence remain untouched.
    """

    language = str(output_language or "").strip()
    primary = language.replace("_", "-").split("-", 1)[0].casefold()
    target_script = _SCRIPT_BY_LANGUAGE.get(primary)
    if not target_script:
        return None

    invalid_fields: list[dict[str, Any]] = []
    for field, value in fields.items():
        for path, text in _semantic_strings(field, value):
            alpha_count = sum(character.isalpha() for character in text)
            if alpha_count < _MIN_FIELD_ALPHA_CHARS:
                continue
            target_count = sum(
                character.isalpha()
                and target_script in unicodedata.name(character, "")
                for character in text
            )
            foreign_phrases = _foreign_lowercase_phrases(
                text,
                target_script=target_script,
            )
            foreign_words = list(
                dict.fromkeys(
                    word
                    for phrase in foreign_phrases
                    for word in phrase
                    if _is_substantive_lowercase_latin(word)
                )
            )[:12]
            if target_count >= _MIN_TARGET_SCRIPT_CHARS and not foreign_words:
                continue
            if target_count < _MIN_TARGET_SCRIPT_CHARS and not foreign_words:
                # Long official names and title-like values may legitimately be
                # entirely in their original script. Wrong-language prose has a
                # run of ordinary lowercase words and is rejected below.
                continue
            invalid = {
                "field": path,
                "alphabetic_chars": alpha_count,
                "target_script_chars": target_count,
            }
            if foreign_words:
                invalid["foreign_lowercase_words"] = foreign_words
                invalid["foreign_prose_phrases"] = [
                    " ".join(phrase) for phrase in foreign_phrases[:6]
                ]
            invalid_fields.append(invalid)
    if not invalid_fields:
        return None
    return {
        "accepted": False,
        "error": "model_authored_prose_language_mismatch",
        "output_language": language,
        "expected_script": target_script.casefold(),
        "invalid_fields": invalid_fields,
        "next_action": (
            "Rewrite only the listed model-authored prose fields in output_language. Preserve IDs, refs, enums, "
            "facts, scope, and limitations. Preserve proper names, official titles, abbreviations, and codes; "
            "translate ordinary foreign words instead of copying source prose."
        ),
    }


def _foreign_lowercase_phrases(
    text: str,
    *,
    target_script: str,
) -> list[list[str]]:
    """Return clear lowercase Latin prose runs, not isolated terms or names."""

    phrases: list[list[str]] = []
    current_words: list[str] = []
    current_substantive_count = 0
    previous_end = 0

    def flush() -> None:
        nonlocal current_words, current_substantive_count
        if current_substantive_count >= _MIN_FOREIGN_PROSE_WORDS:
            phrases.append(current_words)
        current_words = []
        current_substantive_count = 0

    for match in _WORD_RE.finditer(text):
        separator = text[previous_end : match.start()]
        if any(character in ".!?;:\n" for character in separator):
            flush()

        word = match.group(0)
        letters = [character for character in word if character.isalpha()]
        if any(target_script in unicodedata.name(character, "") for character in letters):
            flush()
        elif letters and all(character.isascii() for character in letters):
            if word.islower():
                current_words.append(word)
                if _is_substantive_lowercase_latin(word):
                    current_substantive_count += 1
            elif current_words:
                flush()
        else:
            flush()
        previous_end = match.end()

    flush()
    return phrases


def _is_substantive_lowercase_latin(word: str) -> bool:
    letters = [character for character in word if character.isalpha()]
    return (
        len(letters) >= _MIN_FOREIGN_LOWERCASE_WORD_CHARS
        and all(character.isascii() for character in letters)
        and word.islower()
    )


def _semantic_strings(path: str, value: Any):
    if isinstance(value, str):
        if value.strip():
            yield path, value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _semantic_strings(f"{path}.{key}", item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _semantic_strings(f"{path}[{index}]", item)


__all__ = ["model_authored_language_error"]
