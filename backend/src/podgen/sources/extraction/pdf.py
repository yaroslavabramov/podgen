"""Legacy Marker worker with hard timeout and pypdfium2/PyPDF2 fallbacks."""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .marker_compat import _apply_surya_transformers_compat

logger = logging.getLogger(__name__)


def _marker_sync(pdf_path: str) -> str:
    """Synchronous marker-pdf conversion."""
    if package_version("marker-pdf") != "1.5.5":
        raise RuntimeError("qualified_marker_1_5_5_unavailable_in_worker")
    _apply_surya_transformers_compat()
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    models = create_model_dict()
    converter = PdfConverter(artifact_dict=models)
    result = converter(pdf_path)
    # marker-pdf returns a ConverterResult; the markdown is in .markdown
    return result.markdown


def _run_marker_with_hard_timeout(pdf_path: str, timeout_seconds: int, python_executable=None) -> str:
    """Keep OCR libraries and their wall-clock timeout in a separate process."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = os.path.join(tmp_dir, "marker_output.md")
        cmd = [
            python_executable or sys.executable,
            "-m",
            "podgen.sources.extraction.marker_worker",
            "--input",
            pdf_path,
            "--output",
            output_path,
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"marker subprocess timed out after {timeout_seconds}s") from exc

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            if details:
                raise RuntimeError(f"marker subprocess failed: {details}")
            raise RuntimeError(f"marker subprocess exited with code {completed.returncode}")

        if not os.path.exists(output_path):
            raise RuntimeError("marker subprocess succeeded but produced no output file")
        return Path(output_path).read_text(encoding="utf-8")


def _fallback_pypdfium2(pdf_path: str) -> str:
    """Fallback extraction using pypdfium2 (bundled transitively via marker-pdf)."""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_path)
        pages: list[str] = []
        try:
            for idx in range(len(doc)):
                page = doc.get_page(idx)
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                if text:
                    pages.append(f"## Page {idx + 1}\n\n{text}")
        finally:
            doc.close()
        return "\n\n".join(pages)
    except ImportError:
        logger.warning("pypdfium2 is unavailable")
        return ""
    except Exception:
        logger.warning("pypdfium2 fallback failed", exc_info=True)
        return ""


def _fallback_pypdf2(pdf_path: str) -> str:
    """Fallback extraction using PyPDF2 when pypdfium2 is unavailable."""
    try:
        from PyPDF2 import PdfReader
        from PyPDF2.errors import PdfReadError

        reader = PdfReader(pdf_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                pages.append(f"## Page {i + 1}\n\n{text}")
        return "\n\n".join(pages)
    except ImportError as exc:
        raise RuntimeError(
            "PDF extraction fallback unavailable: neither pypdfium2 nor PyPDF2 can be used. "
            "Install one of them to extract PDF content."
        ) from exc
    except PdfReadError as error:
        raise ValueError("pdf_text_layer_unreadable:" + str(error)) from error


async def extract_pdf_bytes(
    pdf_bytes, *, timeout_seconds=120, source_ref="saved_pdf", python_executable=None, use_marker=True
):
    from .contracts import ExtractedDocument

    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("invalid_pdf_signature")
    failures = []
    with tempfile.TemporaryDirectory(prefix="podgen-source-pdf-") as folder:
        path = Path(folder) / "original.pdf"
        path.write_bytes(pdf_bytes)
        if use_marker:
            try:
                text = await asyncio.to_thread(
                    _run_marker_with_hard_timeout, str(path), timeout_seconds, python_executable
                )
                if text.strip():
                    return ExtractedDocument(
                        text,
                        "marker",
                        "1.5.5",
                        warnings=["ocr_and_reading_order_not_independently_verified"],
                        is_transformation=True,
                    )
                failures.append("marker_empty_output")
            except (TimeoutError, RuntimeError, OSError) as error:
                failures.append("marker:" + str(error))
        else:
            failures.append("marker_disabled_text_layer_only")
        for name, function, package in (
            ("pypdfium2", _fallback_pypdfium2, "pypdfium2"),
            ("PyPDF2", _fallback_pypdf2, "PyPDF2"),
        ):
            try:
                text = await asyncio.to_thread(function, str(path))
                if text.strip():
                    return ExtractedDocument(
                        text,
                        name,
                        package_version(package),
                        warnings=failures + ["pdf_text_layer_only_no_ocr"],
                    )
                failures.append(name + ":empty_output")
            except (ValueError, RuntimeError, OSError) as error:
                failures.append(name + ":" + str(error))
    raise ValueError("pdf_extraction_failed:" + ";".join(failures))


def package_version(name):
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"
