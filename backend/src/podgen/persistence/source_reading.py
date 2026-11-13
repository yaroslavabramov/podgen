"""Share an operation-owned authorization scope across local VerifiedText reads."""

from contextlib import ExitStack, contextmanager

from podgen.persistence.read_authorization import read_operation
from podgen.persistence.text_source import VerifiedText


@contextmanager
def read_sources(documents):
    repositories = {
        id(doc.text.repo): doc.text.repo for doc in documents if isinstance(doc.text, VerifiedText)
    }
    with ExitStack() as stack:
        for repo in repositories.values():
            stack.enter_context(read_operation(repo))
        yield
