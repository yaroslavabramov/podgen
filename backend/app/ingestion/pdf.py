"""PDF text extraction using marker-pdf."""

import asyncio
import logging
import multiprocessing
import os
import queue as queue_module
import tempfile
import time
from pathlib import Path

from app.config import settings
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
    return await extract_pdf_bytes(pdf_bytes, source_ref=file_path)


async def extract_pdf_bytes(pdf_bytes: bytes, *, source_ref: str = "pdf_bytes") -> str:
    """Convert PDF bytes to markdown using marker-pdf.

    Args:
        pdf_bytes: Raw PDF file bytes.
        source_ref: Identifier for logging/error messages.

    Returns:
        Markdown string of the extracted content.
    """
    if not pdf_bytes:
        raise ValueError(f"Downloaded empty PDF from {source_ref}")

    logger.info("Downloaded PDF (%d bytes) from %s", len(pdf_bytes), source_ref)

    # Write to a temp file because marker-pdf operates on file paths
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, "input.pdf")
        with open(input_path, "wb") as f:
            f.write(pdf_bytes)

        markdown = await _convert_with_marker(input_path)

    if not markdown or not markdown.strip():
        raise ValueError(f"marker-pdf returned empty output for {source_ref}")

    return markdown


async def _convert_with_marker(pdf_path: str) -> str:
    """Run marker-pdf conversion in a thread to avoid blocking the event loop."""
    timeout_seconds = max(1, int(settings.pdf_marker_timeout_seconds))
    try:
        return await asyncio.to_thread(
            _run_marker_with_hard_timeout,
            pdf_path,
            timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "marker-pdf conversion timed out after %ss for %s; falling back to pypdfium2/PyPDF2",
            timeout_seconds,
            pdf_path,
        )
        return _fallback_pdf_text(pdf_path)
    except Exception:
        logger.warning(
            "marker-pdf conversion failed for %s; falling back to pypdfium2/PyPDF2",
            pdf_path,
            exc_info=True,
        )
        return _fallback_pdf_text(pdf_path)


def _marker_sync(pdf_path: str) -> str:
    """Synchronous marker-pdf conversion."""
    _apply_surya_transformers_compat()
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    models = create_model_dict()
    converter = PdfConverter(artifact_dict=models)
    result = converter(pdf_path)
    # marker-pdf returns a ConverterResult; the markdown is in .markdown
    return result.markdown


def _marker_subprocess_entry(pdf_path: str, result_queue) -> None:
    """Subprocess target for marker conversion."""
    try:
        markdown = _marker_sync(pdf_path)
        result_queue.put({"ok": True, "markdown": markdown})
    except BaseException as exc:  # pragma: no cover - exercised via parent handling
        try:
            result_queue.put({"ok": False, "error": repr(exc)})
        except BaseException:
            pass


def _run_marker_with_hard_timeout(pdf_path: str, timeout_seconds: int) -> str:
    """Run marker conversion in a separate process with a hard timeout.

    Using a subprocess avoids the default-executor shutdown wait from `asyncio.run`
    in Celery tasks, so total task latency is bounded by this timeout.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_marker_subprocess_entry, args=(pdf_path, result_queue), daemon=True)
    try:
        process.start()
        process.join(timeout_seconds)

        if process.is_alive():
            process.terminate()
            process.join(2)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(1)
            raise TimeoutError(f"marker subprocess timed out after {timeout_seconds}s")

        payload = None
        try:
            payload = result_queue.get_nowait()
        except queue_module.Empty:
            payload = None

        if isinstance(payload, dict):
            if payload.get("ok"):
                return str(payload.get("markdown", ""))
            raise RuntimeError(str(payload.get("error", "marker subprocess failed")))

        if process.exitcode == 0:
            raise RuntimeError("marker subprocess exited without payload")
        raise RuntimeError(f"marker subprocess exited with code {process.exitcode}")
    finally:
        result_queue.close()
        result_queue.join_thread()


def prewarm_marker_models() -> None:
    """Eagerly initialize marker models to avoid first-request cold start."""
    _apply_surya_transformers_compat()
    from marker.models import create_model_dict

    create_model_dict()


def _fallback_pdf_text(pdf_path: str) -> str:
    """Fallback extraction chain for PDF text when marker conversion is unavailable."""
    text = _fallback_pypdfium2(pdf_path)
    if text and text.strip():
        return text
    return _fallback_pypdf2(pdf_path)


def _fallback_pypdfium2(pdf_path: str) -> str:
    """Fallback extraction using pypdfium2 (bundled transitively via marker-pdf)."""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_path)
        pages: list[str] = []
        for idx in range(len(doc)):
            page = doc.get_page(idx)
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            if text:
                pages.append(f"## Page {idx + 1}\n\n{text}")
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


def _apply_surya_transformers_compat() -> None:
    """Patch Surya OCR config init for newer transformers compatibility.

    surya-ocr 0.12.x defines `SuryaOCRConfig.__init__` with mandatory
    `kwargs.pop("encoder")` / `kwargs.pop("decoder")`. Newer transformers call
    `ConfigClass()` during config repr/logging, which raises KeyError and breaks
    marker model initialization.
    """
    try:
        import surya.common.s3 as surya_s3
        from surya.recognition.model.config import DonutSwinConfig, SuryaOCRConfig, SuryaOCRDecoderConfig
        from surya.table_rec.model.config import SuryaTableRecConfig
        from surya.texify.model.config import TexifyConfig
        from surya.layout.model.config import SuryaLayoutConfig
    except Exception:
        return

    _patch_surya_s3_retry_behavior(surya_s3)

    def _patch_get_text_config(cls):
        if getattr(cls, "_podgen_patched_text_config", False):
            return
        original_get_text_config = cls.get_text_config

        def _patched_get_text_config(self, decoder=None, encoder=None):
            # transformers>=4.57 can observe both text_encoder and decoder on
            # Surya composite configs, and default `get_text_config()` becomes
            # ambiguous.
            if decoder is None and encoder is None and hasattr(self, "decoder"):
                decoder_cfg = getattr(self, "decoder", None)
                if decoder_cfg is not None:
                    return decoder_cfg
            return original_get_text_config(self, decoder=decoder, encoder=encoder)

        cls.get_text_config = _patched_get_text_config
        cls._podgen_patched_text_config = True

    _patch_get_text_config(SuryaOCRConfig)
    _patch_get_text_config(SuryaTableRecConfig)
    _patch_get_text_config(TexifyConfig)
    _patch_get_text_config(SuryaLayoutConfig)

    if getattr(SuryaOCRConfig, "_podgen_patched_init", False):
        return
    original_init = SuryaOCRConfig.__init__

    def _patched_init(self, **kwargs):
        if "encoder" not in kwargs or "decoder" not in kwargs:
            kwargs = dict(kwargs)
            kwargs.setdefault("encoder", DonutSwinConfig().to_dict())
            kwargs.setdefault("decoder", SuryaOCRDecoderConfig().to_dict())
        return original_init(self, **kwargs)

    SuryaOCRConfig.__init__ = _patched_init
    SuryaOCRConfig._podgen_patched_init = True


def _patch_surya_s3_retry_behavior(surya_s3_module) -> None:
    """Patch surya S3 downloader retry logic to keep remote path stable.

    Upstream surya mixin mutates `pretrained_model_name_or_path` inside the
    retry loop. If the first attempt fails (e.g. transient reset), next attempt
    can incorrectly request:
    `https://models.datalab.to/<absolute-local-path>/manifest.json` -> 404.
    """
    mixin = surya_s3_module.S3DownloaderMixin
    if getattr(mixin, "_podgen_patched_retry_path", False):
        return

    def _patched_from_pretrained(cls, pretrained_model_name_or_path, *args, **kwargs):
        if not isinstance(pretrained_model_name_or_path, str) or not pretrained_model_name_or_path.startswith("s3://"):
            return super(mixin, cls).from_pretrained(pretrained_model_name_or_path, *args, **kwargs)

        remote_path = pretrained_model_name_or_path.replace("s3://", "")
        cache_dir = Path(surya_s3_module.user_cache_dir("datalab")) / "models"
        local_path = os.path.join(cache_dir, remote_path)
        os.makedirs(local_path, exist_ok=True)

        retries = 3
        delay = 5
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                surya_s3_module.download_directory(remote_path, local_path)
                break
            except Exception as exc:
                last_error = exc
                print(
                    f"Error downloading model from {remote_path}. "
                    f"Attempt {attempt} of {retries}. Error: {exc}"
                )
                if attempt < retries:
                    print(f"Retrying in {delay} seconds...")
                    time.sleep(delay)
        else:
            assert last_error is not None
            print(f"Failed to download {remote_path} after {retries} attempts.")
            raise last_error

        return super(mixin, cls).from_pretrained(local_path, *args, **kwargs)

    mixin.from_pretrained = classmethod(_patched_from_pretrained)
    mixin._podgen_patched_retry_path = True
