"""Scoped SQLite FTS5/BM25 plus a distinct unstemmed names, numbers and phrase branch."""

import re
import sqlite3
import unicodedata
from contextlib import closing
from functools import lru_cache

import snowballstemmer
from langdetect import DetectorFactory, LangDetectException, detect

from . import RetrievalUnavailable

DetectorFactory.seed = 0
TOKEN = re.compile(r"\w+(?:[-./]\w+)*", re.UNICODE)


def words(text):
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


@lru_cache(maxsize=128)
def language(sample):
    try:
        return detect(sample)
    except LangDetectException:
        return "simple"


@lru_cache(maxsize=2)
def stemmer(code):
    return snowballstemmer.stemmer({"en": "english", "ru": "russian"}[code])


def normalize(text, code):
    tokens = words(text)
    return stemmer(code).stemWords(tokens) if code in ("en", "ru") else tokens


def rank(query, texts, languages, limit):
    # This index contains only the allowed chunks. Neither MATCH nor its corpus statistics sees outsiders.
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE scope USING fts5(tokens, tokenize='unicode61')")
            connection.executemany(
                "INSERT INTO scope(rowid,tokens) VALUES(?,?)",
                (
                    (index + 1, " ".join(normalize(text, code)))
                    for index, (text, code) in enumerate(zip(texts, languages, strict=True))
                ),
            )
            terms = sorted({token for code in set(languages) for token in normalize(query, code)})
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in terms)
            if not expression:
                return [], []
            lexical = [
                (row - 1, -score)
                for row, score in connection.execute(
                    "SELECT rowid,bm25(scope) AS score FROM scope WHERE scope MATCH ? ORDER BY score,rowid LIMIT ?",
                    (expression, limit),
                )
            ]
    except sqlite3.Error as error:
        raise RetrievalUnavailable("source_search_fts_unavailable") from error
    query_words = words(query)
    phrase = " " + " ".join(query_words) + " "
    entities = {
        token.casefold()
        for token in TOKEN.findall(query)
        if any(c.isdigit() for c in token) or token[0].isupper()
    }
    exact = []
    for index, text in enumerate(texts):
        tokens = words(text)
        phrase_match = phrase in " " + " ".join(tokens) + " "
        entity_matches = len(entities.intersection(tokens))
        if phrase_match or entity_matches:
            exact.append((index, int(phrase_match) + entity_matches / max(1, len(entities))))
    exact.sort(key=lambda pair: (-pair[1], pair[0]))
    return lexical, exact[:limit]
