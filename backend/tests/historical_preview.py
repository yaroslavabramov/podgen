"""Explicit replay for pre-production synthetic/archived wire tests; never a production default."""

from podgen.runtime.evaluation.preview import preview_request


def replay_preview(*args, **kwargs):
    if kwargs.get("snapshot") is None:
        kwargs.setdefault("behavior_profile", "existing/1")
    return preview_request(*args, **kwargs)
