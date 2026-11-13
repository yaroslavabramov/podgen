"""A final audit releases every owned snapshot for remote source I/O and rechecks withdrawal."""

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.repository import Repository
from podgen.persistence.text_source import verified_text
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reading_audit import lifecycle
from tests.authorization_helpers import withdraw_blob


def test_final_audit_scopes_include_admissions_and_recheck_after_remote_read(repo, scope, monkeypatch):
    project, _ = scope
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    journal = EvaluationJournal(repo, project)
    sources = []
    for raw in (b"SYNTHETIC warm source", b"SYNTHETIC withdrawn remote admission"):
        blob = repo.save_blob(project, io.BytesIO(raw), "text/plain")
        ref = RuntimeRef(record_kind="user_input", record_id=uuid4())
        repo.append_runtime(project, ref, dict(blob=blob.model_dump(mode="json")))
        sources.append(
            SimpleNamespace(ref=ref, blob=blob, text=verified_text(generation, project, ref, blob))
        )
    sources[0].text.verify()
    original = repo.objects
    observed = []

    class Remote:
        def __getattr__(self, name):
            return getattr(original, name)

        def iter_read(self, owner, blob, **options):
            for reader in (repo, generation):
                assert reader._read_authorization.get().transaction is None
            observed.append("released")
            yield from original.iter_read(owner, blob, **options)
            withdraw_blob(repo, project, sources[1].blob)

    generation.objects = Remote()
    materials = SimpleNamespace(documents=[sources[0]], runtime_admissions=[sources[1]])

    def local_validation(journal, invocation, parsed, audit, materials, script):
        assert journal.read(sources[0].ref)
        assert sources[0].text[:]
        assert repo._read_authorization.get().transaction is not None
        assert generation._read_authorization.get().transaction is not None
        return sources[1].text[:]  # Remote admission withdrawal must be rechecked.

    monkeypatch.setattr(lifecycle, "_finalize", local_validation)
    with pytest.raises(AccessDenied):
        lifecycle.finalize(journal, {}, None, None, materials, {})
    assert observed == ["released"]
    assert repo._read_authorization.get() is None
    assert generation._read_authorization.get() is None
