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
    return _pdf_objects(objects)


def _pdf_objects(objects):
    body = b"%PDF-1.4\n"
    offsets = [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + value + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        body
        + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n".encode()
        + str(xref).encode()
        + b"\n%%EOF\n"
    )


def unicode_pdf(text):
    """A real text layer with explicit Unicode mapping, including non-BMP characters."""
    codes = "".join(f"{i:02X}" for i in range(1, len(text) + 1))
    stream = f"BT /F1 12 Tf 72 720 Td <{codes}> Tj ET".encode()
    mapping = "\n".join(
        f"<{i:02X}> <{character.encode('utf-16-be').hex().upper()}>" for i, character in enumerate(text, 1)
    )
    cmap = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        "/CIDSystemInfo << /Registry (Test) /Ordering (Unicode) /Supplement 0 >> def "
        "/CMapName /TestUnicode def /CMapType 2 def "
        "1 begincodespacerange <00> <FF> endcodespacerange "
        f"{len(text)} beginbfchar\n{mapping}\nendbfchar "
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode()
    return _pdf_objects(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Length " + str(len(cmap)).encode() + b" >>\nstream\n" + cmap + b"\nendstream",
        ]
    )
