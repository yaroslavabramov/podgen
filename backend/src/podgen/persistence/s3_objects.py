"""Project/content/media identity, server-checked writes and bounded legacy verification."""

import hashlib

from botocore.exceptions import ClientError

from podgen.domain.errors import ContractError

from .blob_staging import BLOCK_SIZE, StagedObjectStore, verify
from .s3_integrity import checksums, full_checksum
from .s3_publication import create, error_code, missing


class S3ObjectStore(StagedObjectStore):
    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def _address(self, project_id, ref):
        if not ref.storage_key.startswith(f"{project_id}/"):
            raise ContractError("invalid_blob_scope")
        return dict(Bucket=self.bucket, Key=ref.storage_key)

    def _head(self, project_id, ref):
        try:
            return self.client.head_object(**self._address(project_id, ref), ChecksumMode="ENABLED")
        except ClientError as exc:
            if missing(exc):
                return None
            raise

    def publish(self, project_id, staged):
        ref = staged.reference(project_id)
        head = self._head(project_id, ref)
        if head is not None:
            self._verify_head(project_id, ref, staged, head)
            return ref
        try:
            create(self, staged, ref)
        except ClientError as exc:
            if error_code(exc) not in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409"):
                raise
            # A concurrent winner must be independently checked; never overwrite or choose a new key.
            head = self._head(project_id, ref)
            if head is None:
                raise
            self._verify_head(project_id, ref, staged, head)
        return ref

    def verify_existing(self, project_id, ref, staged):
        staged.matches(ref)
        head = self._head(project_id, ref)
        if head is None:
            raise ContractError("blob_integrity_error")
        self._verify_head(project_id, ref, staged, head)

    def _verify_head(self, project_id, ref, staged, head):
        if head["ContentLength"] != ref.byte_length or head.get("ContentType") != ref.media_type:
            raise ContractError("blob_integrity_error")
        checksum = head.get("ChecksumSHA256")
        if checksum is not None and head.get("ChecksumType") != "COMPOSITE" and "-" not in checksum:
            expected = full_checksum(staged)
        elif checksum is not None and "podgen-part-size" in head.get("Metadata", {}):
            try:
                size = int(head["Metadata"]["podgen-part-size"])
            except ValueError as exc:
                raise ContractError("blob_integrity_error") from exc
            _, expected = checksums(staged, size)
        else:
            # Historical objects may have only user metadata or an unrelated checksum algorithm.
            # Neither metadata nor ETag establishes SHA256 integrity.
            for _ in self.iter_read(project_id, ref):
                pass
            return
        if checksum != expected:
            raise ContractError("blob_integrity_error")

    def read(self, project_id, ref):
        result = self.client.get_object(**self._address(project_id, ref))
        try:
            data = result["Body"].read()
        finally:
            result["Body"].close()
        verify(data, ref)
        return data

    def iter_read(self, project_id, ref, *, block_size=BLOCK_SIZE):
        result = self.client.get_object(**self._address(project_id, ref))
        checksum, size = hashlib.sha256(), 0
        try:
            while chunk := result["Body"].read(block_size):
                checksum.update(chunk)
                size += len(chunk)
                yield chunk
        finally:
            result["Body"].close()
        if size != ref.byte_length or checksum.hexdigest() != ref.sha256:
            raise ContractError("blob_integrity_error")
