"""PDF text extraction using marker-pdf."""

import logging
import os
import tempfile

from app.services.storage import get_minio_client, download_file

logger = logging.getLogger(__name__)


async def extract_pdf(file_path: str) -> str:
    """Download a PDF from MinIO and convert to markdown using marker-pdf.

    Args:
        file_path: Object path in MinIO (e.g. "uploads/abc123.pdf").

    Returns:
        Markdown string of the extracted content.
    """
    minio = get_minio_client()
    pdf_bytes = download_file(minio, file_path)

    if not pdf_bytes:
        raise ValueError(f"Downloaded empty PDF from {file_path}")

    logger.info("Downloaded PDF (%d bytes) from %s", len(pdf_bytes), file_path)

    # Write to a temp file because marker-pdf operates on file paths
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "input.pdf")
        with open(input_path, "wb") as f:
            f.write(pdf_bytes)

        markdown = await _convert_with_marker(input_path)

    if not markdown or not markdown.strip():
        raise ValueError(f"marker-pdf returned empty output for {file_path}")

    return markdown


async def _convert_with_marker(pdf_path: str) -> str:
    """Run marker-pdf conversion in a thread to avoid blocking the event loop."""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _marker_sync, pdf_path)


def _marker_sync(pdf_path: str) -> str:
    """Synchronous marker-pdf conversion."""
    try:
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        models = create_model_dict()
        converter = PdfConverter(artifact_dict=models)
        result = converter(pdf_path)
        # marker-pdf returns a ConverterResult; the markdown is in .markdown
        return result.markdown
    except ImportError:
        logger.warning("marker-pdf not available, falling back to PyPDF2")
        return _fallback_pypdf(pdf_path)


def _fallback_pypdf(pdf_path: str) -> str:
    """Fallback extraction using PyPDF2 when marker-pdf is unavailable."""
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(pdf_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                pages.append(f"## Page {i + 1}\n\n{text}")
        return "\n\n".join(pages)
    except ImportError:
        raise RuntimeError(
            "Neither marker-pdf nor PyPDF2 is installed. "
            "Install one of them to extract PDF content."
        )
