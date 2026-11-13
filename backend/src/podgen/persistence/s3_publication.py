"""Conditional immutable S3 writes, including abortable multipart publication."""

from botocore.exceptions import ClientError

from .s3_integrity import FileRange, check_receipt, checksums, encoded, full_checksum, part_size


def error_code(error):
    return str(error.response.get("Error", {}).get("Code", ""))


def missing(error):
    return error_code(error) in ("404", "NoSuchKey", "NotFound")


def create(store, staged, ref):
    size = part_size(staged.byte_length)
    metadata = {"sha256": staged.sha256, "byte-length": str(staged.byte_length)}
    if staged.byte_length <= size:
        with staged.path.open("rb") as source:
            response = store.client.put_object(
                Bucket=store.bucket,
                Key=ref.storage_key,
                Body=source,
                ContentLength=staged.byte_length,
                ContentType=staged.media_type,
                Metadata=metadata,
                IfNoneMatch="*",
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=full_checksum(staged),
            )
        check_receipt(response, full_checksum(staged))
    else:
        multipart(store, staged, ref, size, metadata)


def multipart(store, staged, ref, size, metadata):
    parts, composite = checksums(staged, size)
    address = dict(Bucket=store.bucket, Key=ref.storage_key)
    started = store.client.create_multipart_upload(
        **address,
        ContentType=staged.media_type,
        Metadata={**metadata, "podgen-part-size": str(size)},
        ChecksumAlgorithm="SHA256",
        ChecksumType="COMPOSITE",
    )
    upload = {**address, "UploadId": started["UploadId"]}
    completed, offset, succeeded = [], 0, False
    try:
        for number, (length, digest) in enumerate(parts, 1):
            checksum = encoded(digest)
            with FileRange(staged.path, offset, length) as source:
                response = store.client.upload_part(
                    **upload,
                    PartNumber=number,
                    Body=source,
                    ContentLength=length,
                    ChecksumAlgorithm="SHA256",
                    ChecksumSHA256=checksum,
                )
            check_receipt(response, checksum)
            completed.append(dict(PartNumber=number, ETag=response["ETag"], ChecksumSHA256=checksum))
            offset += length
        response = store.client.complete_multipart_upload(
            **upload,
            MultipartUpload={"Parts": completed},
            IfNoneMatch="*",
            ChecksumType="COMPOSITE",
            MpuObjectSize=staged.byte_length,
        )
        check_receipt(response, composite)
        succeeded = True
    finally:
        if not succeeded:
            # This upload ID is ours; the final content key may belong to another publisher.
            try:
                store.client.abort_multipart_upload(**upload)
            except ClientError as exc:
                if error_code(exc) != "NoSuchUpload":
                    raise
