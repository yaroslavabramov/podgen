# Acquiring and extracting a source

`podgen.sources.acquisition.service.acquire_source` takes an `AcquisitionRequest` and returns an `AcquisitionResult`. It fetches the document behind a reported URL, records the bytes and produces a readable representation where possible. An exact citation requires a separate reading of that representation.

## Profile and rights

Use a production repository for the project and existing `Policy` references for storage and publication. The example assumes the project, repository, producer and policy references are already available.

```python
from uuid import uuid4
from podgen.domain.contracts.common import CodeProducer
from podgen.domain.contracts.evidence import SourceRights
from podgen.sources.acquisition.contracts import (
    VERSION, AcquisitionProfile, AcquisitionRequest,
)
from podgen.sources.acquisition.service import acquire_source

operation = uuid4()
producer = CodeProducer(
    kind="code", component="source-acquisition-setup",
    build_id=VERSION, operation_id=operation,
)
profile = repo.create(
    project_id, "ProcessingProfile",
    dict(
        profile_kind="extraction", implementation=VERSION, version="1",
        configuration=AcquisitionProfile().model_dump(mode="json"),
        input_profile_refs=[],
    ),
    producer=producer, operation_id=operation,
)
rights = SourceRights(
    access_class="project",
    storage_policy_ref=storage_policy_ref,
    publication_policy_ref=publication_policy_ref,
)
request = AcquisitionRequest(
    operation_id=uuid4(), source_id=uuid4(), url=reported_url,
    profile_ref=profile.ref, rights=rights, language="und",
)
result = await acquire_source(repo, project_id, request)
```

Retry with the same request, including its operation ID. The runtime reuses the saved original and extractor output. A changed request with a reused ID is rejected. Concurrent acquisition of the same operation returns `source_acquisition_busy`. Fetching a new version of a URL requires a new operation.

## What gets saved

The original HTTP body and acquisition record are saved first. The record captures requested and final URLs, redirect chain, response headers, status and acquisition time. Streaming limits bound the response and its decompressed body. Connections use a validated destination IP and ignore environment proxies. Cross-origin redirects drop credentials.

`SourceRevision` points to those exact bytes. The extractor receives the original directly or reads it from object storage on recovery. Its pinned `ProcessingProfile` records the extractor, version and configuration hash. The full extractor output is retained. Paragraph offsets address that text. DOM or PDF locations require an actual anchor from the extractor.

`AcquisitionStore.publication` publishes the original revision and checkpoint atomically. A second short transaction publishes metadata, the extractor profile, representation and outcome. Extracted text survives rollback of that transaction, allowing retry without a second fetch or extraction. Legacy checkpoints are still readable. The underlying `archive_write` transaction uses repeatable read, a project fence and a shared membership lock. A serialization retry repeats only the rolled-back SQL work. Byte processing stays outside the transaction.

The artifact writer batches distinct dependency references and verifies subtype, lineage, current access, withdrawal and publication policy. Replaying an immutable revision leaves its links intact. Blob and selector checks are prepared outside SQL. A byte descriptor establishes content identity. Authorization is checked again at publication. [Blob publication](blob-publication.md)

MediaWiki responses, Jina responses, discovered PDFs and browser captures each have their own acquisition record and bytes. The result distinguishes technical availability, an unconfirmed document and a failed extraction. Check diagnostics for login or consent pages, challenges, publisher shells and error responses. A successful read can still describe an incomplete or factually incorrect document.

## Re-extracting a saved revision

```python
from podgen.sources.acquisition.contracts import ReExtractionRequest
from podgen.sources.acquisition.reprocess import reprocess_source

result = await reprocess_source(
    repo, project_id,
    ReExtractionRequest(
        operation_id=uuid4(),
        source_revision_ref=saved_source_revision_ref,
        profile_ref=profile.ref,
        reason="Install the missing local extractor",
    ),
)
```

`reprocess_source` uses the original named by the revision and the current access decision. It records the new result alongside the earlier failure. Repeating the same re-extraction operation reuses its result.

## Dependencies and fallback paths

- The core environment includes Trafilatura, pypdfium2 and PyPDF2. In `marker_with_text_fallback` mode a time-limited Marker worker runs before text-layer fallback. `text_layer_only` skips OCR. A missing worker, timeout or empty extraction is visible in diagnostics.
- The OCR worker has a separate environment and lockfile. Install it with `uv sync --project backend/ocr --python 3.13`, provision the model weights and set `marker_python_executable` to the absolute path of that environment's Python. The worker project pins Marker 1.5.5, Surya 0.12.1 and transformers 4.57.6 on Python 3.12–3.13. [OCR worker](../../backend/ocr/pyproject.toml)
- New acquisition profiles enable the Chromium fallback. Install the backend's `browser` extra and Chromium (`uv run --project backend --extra browser playwright install chromium`) to make the worker available. The browser guard checks navigation and subrequests, blocks cross-origin assets and service workers and limits the stored result. A rendered capture is a separately identified representation of the page.
- Jina is opt-in via `enable_jina` and reads its key from `jina_credential_env` (default `JINA_API_KEY`). When it points to a PDF, Podgen records that subsequent fetch as a separate original. The acquisition profile pinned in a historical run determines which routes were available there.
