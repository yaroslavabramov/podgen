from uuid import uuid4

from podgen.sources.acquisition.contracts import (
    VERSION,
    AcquisitionProfile,
    AcquisitionRequest,
    FetchedDocument,
)
from tests.factories import Factory

ARTICLE = (
    "A detailed document explains the operating mechanism, chronology, evidence, consequences and limits. "
    * 15
)


def setup(repo, scope, producer, **changes):
    factory = Factory(repo, *scope, producer)
    policy = factory.make("Policy", dict(policy_kind="data", rules={"test_only": True}))
    config = AcquisitionProfile(**changes)
    profile = factory.make(
        "ProcessingProfile",
        dict(
            profile_kind="extraction",
            implementation=VERSION,
            version="1",
            configuration=config.model_dump(mode="json"),
            input_profile_refs=[],
        ),
    )
    request = AcquisitionRequest(
        operation_id=uuid4(),
        source_id=uuid4(),
        url="https://example.com/article",
        profile_ref=profile.ref,
        rights=dict(access_class="project", storage_policy_ref=policy.ref, publication_policy_ref=policy.ref),
        language="en",
    )
    return request, config


def document(
    body=ARTICLE,
    *,
    url="https://example.com/article",
    resolved=None,
    status=200,
    content_type="text/plain",
    redirects=None,
):
    return FetchedDocument(
        url,
        resolved or url,
        redirects or [],
        status,
        {"content-type": content_type},
        body.encode() if isinstance(body, str) else body,
        "2026-11-13T00:00:00Z",
    )


class Fetcher:
    def __init__(self, documents=None):
        self.documents = documents or {"https://example.com/article": document()}
        self.calls = []

    async def fetch(self, url, profile, *, headers=None):
        self.calls.append((url, headers))
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        return value


def simple_pdf(text="The saved PDF document contains searchable original text."):
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    body = b"%PDF-1.4\n"
    offsets = [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 6\n0000000000 65535 f \n"
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return body + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
