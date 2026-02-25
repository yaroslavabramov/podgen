from app.config import settings
from app.services.embeddings import delete_points_by_source_id


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.delete_calls: list[dict] = []

    def delete(self, **kwargs) -> None:
        self.delete_calls.append(kwargs)


def test_delete_points_by_source_id_uses_source_filter(monkeypatch) -> None:
    client = _FakeQdrantClient()
    monkeypatch.setattr("app.services.embeddings.get_qdrant_client", lambda: client)
    monkeypatch.setattr("app.services.embeddings.ensure_collection", lambda: None)

    delete_points_by_source_id("source-123")

    assert len(client.delete_calls) == 1
    call = client.delete_calls[0]
    assert call["collection_name"] == settings.qdrant_collection

    selector = call["points_selector"]
    assert selector.filter.must
    condition = selector.filter.must[0]
    assert condition.key == "source_id"
    assert condition.match.value == "source-123"
