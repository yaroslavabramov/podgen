"""Content-addressed Unicode ranges remain bounded and reauthorize even cached bytes."""

import io

import pytest

from podgen.domain.errors import AccessDenied
from podgen.persistence.database import query
from podgen.persistence.text_source import verified_text
from podgen.runtime.evaluation.journal import EvaluationJournal


def test_large_unicode_stream_never_reads_whole_blob_and_cached_ranges_recheck_access(
    repo, scope, monkeypatch
):
    text = "Ёжик 🚀 exact original.\r\n" * 100000
    blob = repo.save_blob(scope[0], io.BytesIO(text.encode()), "text/plain; charset=utf-8")
    journal = EvaluationJournal(repo, scope[0])
    ref = journal.record(dict(text_blob=blob.model_dump(mode="json")))
    sizes = []
    original = repo.objects.iter_read

    def chunks(*args, **kwargs):
        for chunk in original(*args, **kwargs):
            sizes.append(len(chunk))
            yield chunk

    monkeypatch.setattr(repo.objects, "iter_read", chunks)
    monkeypatch.setattr(repo.objects, "read", lambda *args: pytest.fail("whole-blob read is forbidden"))
    value = verified_text(repo, scope[0], ref, blob)
    assert sizes == []
    assert len(value) == len(text) and value[2390:9610] == text[2390:9610]
    assert max(sizes) <= 1024 * 1024 and len(sizes) > 1
    assert value.find("🚀 exact", 16000) == text.find("🚀 exact", 16000)
    before = len(sizes)
    assert verified_text(repo, scope[0], ref, blob) == value
    assert value[-50:] == text[-50:] and len(sizes) == before
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        value[:10]
