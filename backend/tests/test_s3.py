"""Real S3-compatible provider test; skipped explicitly without endpoint configuration."""

import io
import os
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.persistence.objects import S3ObjectStore


def test_real_s3_complete_bytes_checksums_versions_and_repository(database, scope, repo, producer):
    endpoint = os.environ.get("PODGEN_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("PODGEN_TEST_S3_ENDPOINT is unset: real S3 integration not run")
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="podgen-test",
        aws_secret_access_key="podgen-test-only-password",
        region_name="us-east-1",
    )
    bucket = "podgen-wp01-" + uuid4().hex
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    objects = S3ObjectStore(client, bucket)
    payload = ("ТЕСТОВЫЕ байты 🛰️\0е\u0301\n" * 100000).encode()
    first = objects.put(scope[0], io.BytesIO(payload), "application/octet-stream")
    second = objects.put(
        scope[0],
        io.BytesIO(payload),
        "application/octet-stream",
        expected_sha256=first.sha256,
        expected_length=len(payload),
    )
    assert first == second
    assert objects.read(scope[0], first) == payload
    assert objects.read(scope[0], second) == payload
    assert client.head_object(Bucket=bucket, Key=first.storage_key)["VersionId"]
    with pytest.raises(ContractError, match="blob_checksum_mismatch"):
        objects.put(scope[0], io.BytesIO(payload), "application/octet-stream", expected_sha256="0" * 64)
    from podgen.persistence.repository import Repository
    from tests.factories import Factory

    actual = Repository(database, objects, repo.actor_id, purpose="evaluation")
    f = Factory(actual, *scope, producer).setup()
    source, blob = f.source("ТЕСТОВЫЙ source через реальный PostgreSQL и MinIO 🛰️")
    assert actual.read_blob(scope[0], source.ref, blob).decode().endswith("🛰️")
