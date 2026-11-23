# Blob publication

`Repository.save_blob` owns authorization and database publication. The object store owns immutable bytes and storage integrity. `blob_staging` owns one temporary file for the operation; it reads the caller's stream once, in 1 MiB blocks, and verifies any supplied SHA256/length before network I/O. Temporary files are removed on normal completion and exceptions.

The repository checks membership before accepting bytes, then checks current membership, evaluator classification and revocation after staging. It looks up `(project, SHA256, media type)` before uploading. After storage work it checks authorization again before publishing or returning the handle. No S3 call runs inside a database transaction. Public reads retain their existing authorization and full SHA256 verification.

New keys contain project, complete content SHA256 and complete media-type SHA256. Existing database references, including historical random S3 keys and filesystem paths, remain unchanged. Content identity never confers access. A different project or media type has its own key and database row.

## Integrity and concurrency

For new S3 objects, `PutObject` sends an explicit SHA256 checksum and `IfNoneMatch='*'`. Large objects use sequential multipart upload with bounded seekable file ranges. Each part has a server-validated SHA256. Completion is conditional and its composite SHA256 must match the locally computed checksum of part digests. The multipart metadata records actual part boundaries; the complete input SHA256 is checked while calculating them. ETags are only opaque part handles.

A preexisting object is checked with `HeadObject(ChecksumMode='ENABLED')`: length, media type and the server's checksum must match the incoming staged content. A known checksum mismatch is an error, never a reason to overwrite. Multipart checksums are compared using the stored part boundaries. Historical objects without usable server SHA256 require one bounded streaming GET; user metadata or an ETag alone cannot establish integrity. This fallback never uploads a replacement.

Two concurrent writers may both transfer bytes, particularly multipart parts, but conditional completion permits only one finalized object/version. The losing upload is aborted and the winner is verified. A 409 without an available winner is an explicit failure; a later operation retries the same content key. Lost responses and failed DB publication likewise reuse that key instead of creating a new random object. Filesystem publication uses an atomic hard link, checks stored bytes in bounded chunks, and fsyncs the parent chain including the store root.

## Failure boundaries

A successful object write followed by failed or denied database publication can leave **one unregistered content-addressed object**. A retry verifies and reuses it. It is deliberately not deleted on failure: another publisher may already reference the same object. Rolling deployment alongside an old random-key writer can also leave that one unused deterministic object when the legacy writer wins the DB constraint. This change prevents repeated duplicate accumulation; it does not implement garbage collection or remove historical orphans.

Owned multipart uploads are aborted on ordinary errors, including losing a conditional race. Process death, a lost create-upload response, or a failed abort can leave incomplete parts. Deployments need an S3 `AbortIncompleteMultipartUpload` lifecycle rule. A process killed during local staging can leave `.upload-*` files; cleanup of abandoned staging and unregistered objects still requires reconciliation with a safety window. The existing platform lifecycle contract remains in effect.

The S3-compatible endpoint must support conditional PUT/complete, SHA256 part/object receipts and checksum-enabled HEAD. New writes fail closed if it omits required integrity receipts; there is no silent weakening for an incompatible endpoint. The tested SDK floor is boto3/botocore 1.43.124. HEAD requires permissions sufficient to distinguish a missing key and retrieve stored checksums (including applicable KMS permissions).

## Evidence

A historical comparison recorded 4 MiB and 64 MiB uploads against the original implementation at `31b545cb`. On the 64 MiB case, first publication went from 64 MiB uploaded and 128 MiB downloaded to 64 MiB uploaded and zero downloaded. A duplicate issued one HEAD, transferred no object body and reused one stored object. Integrated SQL-double counts were eight statements on first save and seven on duplicate. A loopback HTTP service separately checked native SDK checksums and multipart ordering. These results describe the measured doubles and loopback transport. Production PostgreSQL and an external S3 service were outside that trial.

Protocol references: [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html), [server-validated checksums](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html), [multipart checksum tutorial](https://docs.aws.amazon.com/AmazonS3/latest/userguide/tutorial-s3-mpu-additional-checksums.html), [current multipart limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html). Reviewed 2026-11-13.
