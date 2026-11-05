from typing import BinaryIO, Protocol
from uuid import UUID

from .contracts.common import ArtifactRef, ArtifactRevision, BlobRef


class ObjectStore(Protocol):
    def put(
        self,
        project_id: UUID,
        stream: BinaryIO,
        media_type: str,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> BlobRef: ...
    def read(self, project_id: UUID, ref: BlobRef) -> bytes: ...


class ArtifactRepository(Protocol):
    def get(self, project_id: UUID, ref: ArtifactRef) -> ArtifactRevision: ...
    def create(self, project_id: UUID, kind: str, payload: dict, **kwargs) -> ArtifactRevision: ...
