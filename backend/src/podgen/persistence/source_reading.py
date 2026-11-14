"""Share an operation-owned authorization scope across local VerifiedText reads."""

from contextlib import ExitStack, contextmanager

from podgen.persistence.read_authorization import read_operation
from podgen.persistence.text_source import VerifiedText


@contextmanager
def read_sources(documents):
    documents = tuple(documents)
    repositories = {
        id(doc.text.repo): doc.text.repo for doc in documents if isinstance(doc.text, VerifiedText)
    }
    with ExitStack() as stack:
        for repo in repositories.values():
            stack.enter_context(read_operation(repo))
        groups = {}
        for doc in documents:
            text = doc.text
            if isinstance(text, VerifiedText) and text.prepared is not None:
                store, run_id = text.prepared
                key = (id(store), run_id, text.project)
                groups.setdefault(key, (text.repo, store, []))[2].append((text.reference, text.blob))
        from .execution.text import authorize_texts

        for (_, run_id, project), (repo, store, bindings) in groups.items():
            authorize_texts(repo, store, run_id, project, bindings)
        yield
