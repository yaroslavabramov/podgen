"""Local, versioned retrieval projections over admitted frozen source representations."""


class RetrievalUnavailable(Exception):
    """A technical failure, never an empty search result or permission to downgrade retrieval."""


class QueryRejected(ValueError):
    pass
