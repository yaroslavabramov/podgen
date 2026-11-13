# Complete save_blob measurement

Baseline: original `objects.py` and `repository.py` loaded directly from commit `31b545cb`. Candidate: the production `Repository.save_blob` and storage classes in this change. Both execute against the same counting SQL double and disk-backed S3 double. The input is generated as a stream, rather than allocated in full. No PostgreSQL, cloud access or paid API calls.

`measure.py` and `measurements.json` preserve the procedure and results. Each size is saved twice, including staging, authorization, dedup, storage and DB publication. Values below are per operation.

| Operation | Previous upload / download | New upload / download | Previous objects after two saves | New objects |
|---|---:|---:|---:|---:|
| First 4 MiB save | 4 / 8 MiB | 4 / 0 MiB | 2 | 1 |
| Duplicate 4 MiB save | 4 / 8 MiB | 0 / 0 MiB | 2 | 1 |
| First 64 MiB save | 64 / 128 MiB | 64 / 0 MiB | 2 | 1 |
| Duplicate 64 MiB save | 64 / 128 MiB | 0 / 0 MiB | 2 | 1 |

Previously every save called managed upload once and full GET twice; one GET occurred inside the DB transaction. The candidate's first 4 MiB save uses HEAD + PUT. The first 64 MiB save uses HEAD, create multipart, four part uploads and conditional completion. Each duplicate uses one HEAD. No candidate save performs a GET or network operation inside a transaction. Historical objects lacking server SHA256 still need one streaming GET; a separate regression proves reuse of the exact old key and bounded 1 MiB reads.

SQL statements increase from 7 to 10 for a new save and 9 for a duplicate. This is constant work: fresh identity authorization after staging and again after network I/O replaces the previous late-only identity check. The lookup uses the existing unique `(project_id, sha256, media_type)` index. These counts are statement executions, not PostgreSQL latency/query plans.

Final integration note: the subsequent shared blob-access validator combines the
two identity lookups, so the current counts are **8 for a new save and 7 for a
duplicate**. Exact-count assertions passed for empty, small and multipart saves;
the original measurement JSON above retains its original 10/9 counts. Data-flow
and network behavior did not change. The updated SQL double still supports the
baseline's separate lookups. All 36 storage tests passed after integration; see
[the overall audit report](../2026-11-13-engineering-audit/README.md).

Peak Python allocations measured by `tracemalloc`: previous ~4.14 MiB for a 4 MiB object and ~64.14 MiB for a 64 MiB object; candidate ~2.27 MiB on first publication and ~2.14 MiB on duplicate. These are traced allocations, not RSS or filesystem cache. Multipart SHA256 calculation rereads the local staged file in bounded blocks; it does not download the object or hold a part in memory. Original input bytes are read once per operation.

## Actual SDK transport

Beyond the storage double, boto3/botocore 1.43.124 sent a real 17 MiB multipart request to a loopback HTTP server, which independently checked each transmitted SHA256, part order, total length and conditional-completion headers. The duplicate sent HEAD only. The SDK requested `FileRange.read(1048576)` 19 times and `read(131072)` 138 times; it never requested an unbounded read. Peak traced allocations were 2,741,020 bytes (~2.61 MiB). Native Stubber tests also validate single-part and multipart parameter/response shapes, including `IfNoneMatch`, `ChecksumType` and `MpuObjectSize`.

The result supports bounded memory for this installed SDK and tested transport, not arbitrary third-party callers of a file object or untested SDK versions. `FileRange` preserves ordinary seek/read semantics for SDK signing and retries.

## Coverage and limits

36 focused tests cover sequential/concurrent dedup, empty objects, media/project separation, expected SHA/length mismatch, corrupt server checksums/length/media/part boundaries, legacy content corruption, lost success responses, failed DB publication, multipart abort, membership/revocation/classification withdrawal, actual SDK transport, multipart size boundaries and filesystem fsync through the store root. Concurrent multipart saves may both transfer parts; only one final object survives and the losing multipart is aborted. The SQL double does not validate PostgreSQL transaction scheduling or constraints; real endpoint encryption, provider-specific checksum behavior, crash recovery and very large objects remain deployment/integration checks.

Known lifetime limits are explicit in [blob-publication.md](../../../../docs/podcast-production/blob-publication.md): failed DB publication can retain one unregistered deterministic object; process death can leave incomplete multipart uploads or staging files. No GC or deletion of historical objects is claimed.

Run focused checks:

```sh
PYTHONPATH=backend/src:backend backend/.venv/bin/pytest -q \
  backend/tests/test_blob_publication.py \
  backend/tests/test_s3_publication_sdk.py \
  backend/tests/test_s3_publication_transport.py
PYTHONPATH=backend/src:backend backend/.venv/bin/python \
  evals/reports/2026-11-13-s3-publication/measure.py
```

PostgreSQL integration nodes below subsequently passed in root's full integration
run on a fresh database:

```sh
backend/.venv/bin/pytest -q \
  backend/tests/test_blob_publication_postgres.py \
  backend/tests/test_repository.py::test_blob_full_bytes_checksum_scope_and_revoked_ancestor \
  backend/tests/test_cto_privacy.py::test_access_revocation_blocks_old_new_and_derived_blob_aliases \
  backend/tests/test_evaluator_bytes.py::test_gold_bytes_stay_evaluator_only_through_existing_new_wrappers_and_dedup
```

`test_s3.py` additionally exercises the configured real S3-compatible endpoint and versioning when its endpoint environment is available; its expected duplicate identity now matches the publication contract.
