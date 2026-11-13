"""Extract locally from bytes already saved as one SourceRevision."""

import json

from podgen.sources.acquisition.http import decoded_body

from .contracts import ExtractedDocument
from .html import _strip_html, _title_from_pdf_url
from .pdf import extract_pdf_bytes, package_version
from .quality import assess_web_fetch_health, resolve_web_title
from .wikipedia import _extract_wikipedia_revision_date, _wikipedia_sections_to_markdown


def decode_text(body, headers):
    import httpx

    return httpx.Response(
        200, headers={k: v for k, v in headers.items() if k != "content-encoding"}, content=body
    ).text


class DocumentExtractors:
    async def extract(self, document, profile, *, role="original"):
        body = decoded_body(document, profile.max_bytes)
        content_type = document.headers.get("content-type", "").lower()
        if body.startswith(b"%PDF-"):
            result = await extract_pdf_bytes(
                body,
                timeout_seconds=profile.marker_timeout_seconds,
                source_ref=document.resolved_url,
                python_executable=profile.marker_python_executable,
            )
            return ExtractedDocument(
                **(result.__dict__ | {"title": _title_from_pdf_url(document.resolved_url)})
            )
        if "application/pdf" in content_type:
            raise ValueError("invalid_pdf_signature")
        raw = decode_text(body, document.headers)
        if role == "mediawiki":
            pages = json.loads(raw).get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing") or not pages[0].get("extract"):
                raise ValueError("mediawiki_no_article_extract")
            row = pages[0]
            return ExtractedDocument(
                _wikipedia_sections_to_markdown(row["extract"]),
                "mediawiki",
                "1",
                title=row.get("title"),
                published_date=_extract_wikipedia_revision_date(row),
                warnings=["api_extract_coverage_not_proven_complete"],
            )
        if role == "jina":
            from .jina import _extract_jina_published_date, _extract_jina_title

            return ExtractedDocument(
                raw,
                "jina",
                "reader-markdown/1",
                title=_extract_jina_title(raw),
                published_date=_extract_jina_published_date(raw),
                coverage="partial",
                warnings=["remote_reader_representation_not_original_html"],
            )
        if "html" not in content_type and not raw.lstrip().lower().startswith(
            ("<html", "<!doctype", "<body", "<article")
        ):
            if not content_type.startswith("text/"):
                raise ValueError("source_unsupported_content_type:" + content_type)
            return ExtractedDocument(raw, "text", "utf8-decoding/1")
        return extract_html(raw, document.resolved_url, profile.min_text_chars)


def extract_html(html, url, minimum):
    import trafilatura

    metadata = trafilatura.extract_metadata(html)
    warnings, candidates = [], []
    for preference in ("recall", "balanced", "precision"):
        options = dict(
            output_format="markdown", include_comments=False, include_tables=True, include_links=False
        )
        if preference != "balanced":
            options["favor_" + preference] = True
        try:
            candidate = trafilatura.extract(html, **options)
        except Exception as error:
            warnings.append("trafilatura_" + preference + ":" + type(error).__name__)
            continue
        if candidate and candidate.strip():
            candidates.append(candidate)
            health = assess_web_fetch_health(candidate, min_chars=minimum)
            if health.state == "ok":
                return ExtractedDocument(
                    candidate,
                    "trafilatura_" + preference,
                    package_version("trafilatura"),
                    title=resolve_web_title(metadata.title if metadata else None, text=candidate, url=url),
                    published_date=metadata.date if metadata else None,
                    warnings=warnings,
                )
    # The coverage-first legacy extractor keeps a nonempty result even when short.
    if candidates:
        text, title = max(candidates, key=len), metadata.title if metadata else None
        name = "trafilatura_unconfirmed"
    else:
        text, title = _strip_html(html)
        name = "stdlib_html"
        warnings.append("trafilatura_empty_using_visible_html")
    return ExtractedDocument(
        text,
        name,
        package_version("trafilatura"),
        title=resolve_web_title(title, text=text, url=url),
        published_date=metadata.date if metadata else None,
        warnings=warnings,
    )
