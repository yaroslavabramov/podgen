"""Validate calls against the installed botocore service model, without external I/O."""

import io
from uuid import uuid4

import boto3
import pytest
from botocore.stub import ANY, Stubber

from podgen.domain.errors import ContractError
from podgen.persistence.blob_staging import stage_blob
from podgen.persistence.objects import S3ObjectStore
from podgen.persistence.s3_integrity import (
    MAX_OBJECT,
    MAX_PART,
    MAX_PARTS,
    FileRange,
    checksums,
    encoded,
    full_checksum,
    part_size,
)


@pytest.mark.parametrize("multipart", [False, True])
def test_conditional_publication_matches_installed_sdk_contract(multipart):
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    store, project = S3ObjectStore(client, "bucket"), uuid4()
    raw = b"x" * (17 * 1024 * 1024 if multipart else 12)
    with stage_blob(io.BytesIO(raw), "text/plain") as staged, Stubber(client) as stub:
        ref = staged.reference(project)
        address = dict(Bucket="bucket", Key=ref.storage_key)
        stub.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={**address, "ChecksumMode": "ENABLED"},
        )
        metadata = {"sha256": staged.sha256, "byte-length": str(staged.byte_length)}
        if not multipart:
            stub.add_response(
                "put_object",
                {"ChecksumSHA256": full_checksum(staged)},
                {
                    **address,
                    "Body": ANY,
                    "ContentLength": len(raw),
                    "ContentType": "text/plain",
                    "Metadata": metadata,
                    "IfNoneMatch": "*",
                    "ChecksumAlgorithm": "SHA256",
                    "ChecksumSHA256": full_checksum(staged),
                },
            )
        else:
            size = part_size(len(raw))
            parts, composite = checksums(staged, size)
            stub.add_response(
                "create_multipart_upload",
                {"UploadId": "upload"},
                {
                    **address,
                    "ContentType": "text/plain",
                    "Metadata": {**metadata, "podgen-part-size": str(size)},
                    "ChecksumAlgorithm": "SHA256",
                    "ChecksumType": "COMPOSITE",
                },
            )
            completed = []
            for index, (length, sha) in enumerate(parts, 1):
                response = dict(ETag=f"opaque-{index}", ChecksumSHA256=encoded(sha))
                stub.add_response(
                    "upload_part",
                    response,
                    {
                        **address,
                        "UploadId": "upload",
                        "PartNumber": index,
                        "Body": ANY,
                        "ContentLength": length,
                        "ChecksumAlgorithm": "SHA256",
                        "ChecksumSHA256": encoded(sha),
                    },
                )
                completed.append(dict(PartNumber=index, **response))
            stub.add_response(
                "complete_multipart_upload",
                {"ChecksumSHA256": composite, "ChecksumType": "COMPOSITE"},
                {
                    **address,
                    "UploadId": "upload",
                    "MultipartUpload": {"Parts": completed},
                    "IfNoneMatch": "*",
                    "ChecksumType": "COMPOSITE",
                    "MpuObjectSize": len(raw),
                },
            )
        assert store.publish(project, staged) == ref
        stub.assert_no_pending_responses()


def test_multipart_checksums_reject_modified_staging_and_preserve_range_semantics(tmp_path):
    raw = b"a" * (17 * 1024 * 1024)
    with stage_blob(io.BytesIO(raw), "text/plain") as staged:
        staged.path.write_bytes(b"b" * len(raw))
        with pytest.raises(ContractError, match="blob_integrity_error"):
            checksums(staged, part_size(len(raw)))
    path = tmp_path / "parts"
    path.write_bytes(b"beforeDATAafter")
    with FileRange(path, 6, 4) as body:
        assert body.seek(0, 2) == 4
        assert body.seek(0) == 0 and body.read(2) == b"DA"
        assert body.seek(-1, 1) == 1 and body.read() == b"ATA"
        assert body.read() == b""
        with pytest.raises(ValueError):
            body.seek(5)


def test_part_size_supports_current_s3_limits_without_allocating_parts():
    assert part_size(MAX_OBJECT) == MAX_PART
    assert (MAX_OBJECT + part_size(MAX_OBJECT) - 1) // part_size(MAX_OBJECT) == MAX_PARTS
    with pytest.raises(ContractError, match="blob_s3_size_limit"):
        part_size(MAX_OBJECT + 1)
