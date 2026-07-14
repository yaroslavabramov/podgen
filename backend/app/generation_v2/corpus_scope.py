from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CorpusScope:
    revision: int = 1
    attempt: int = 1


def scope_for_run_view(run_view: Any | None) -> CorpusScope:
    return CorpusScope(
        revision=max(1, int(getattr(run_view, "revision", 1) or 1)),
        attempt=max(1, int(getattr(run_view, "attempt", 1) or 1)),
    )


def set_session_corpus_scope(db: Any, generation_run_id: uuid.UUID, run_view: Any) -> CorpusScope:
    scope = scope_for_run_view(run_view)
    scopes = _session_scopes(db, create=True)
    if scopes is not None:
        scopes[str(generation_run_id)] = scope
    return scope


def session_corpus_scope(db: Any, generation_run_id: uuid.UUID) -> CorpusScope:
    return session_corpus_scope_or_none(db, generation_run_id) or CorpusScope()


def session_corpus_scope_or_none(db: Any, generation_run_id: uuid.UUID) -> CorpusScope | None:
    scopes = _session_scopes(db, create=False)
    if scopes is None:
        return None
    return scopes.get(str(generation_run_id))


def _session_scopes(db: Any, *, create: bool) -> dict[str, CorpusScope] | None:
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        if create:
            return info.setdefault("v2_corpus_scopes", {})
        scopes = info.get("v2_corpus_scopes")
        return scopes if isinstance(scopes, dict) else None
    return None


__all__ = [
    "CorpusScope",
    "scope_for_run_view",
    "session_corpus_scope",
    "session_corpus_scope_or_none",
    "set_session_corpus_scope",
]
