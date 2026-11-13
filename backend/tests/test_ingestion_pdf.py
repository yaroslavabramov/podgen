import subprocess
from pathlib import Path

import pytest

from podgen.sources.extraction import pdf


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), RuntimeError("boom")])
@pytest.mark.asyncio
async def test_marker_failure_preserves_diagnostic_and_uses_text_layer(monkeypatch, failure):
    monkeypatch.setattr(pdf, "_run_marker_with_hard_timeout", lambda *args: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(pdf, "_fallback_pypdfium2", lambda path: "Fallback text")
    result = await pdf.extract_pdf_bytes(b"%PDF-saved-original")
    assert result.text == "Fallback text" and result.name == "pypdfium2"
    assert any(str(failure) in warning for warning in result.warnings)
    assert "pdf_text_layer_only_no_ocr" in result.warnings


@pytest.mark.asyncio
async def test_marker_success_does_not_run_fallback(monkeypatch):
    monkeypatch.setattr(pdf, "_run_marker_with_hard_timeout", lambda *args: "Full marker output")
    monkeypatch.setattr(pdf, "_fallback_pypdfium2", lambda path: pytest.fail("unexpected fallback"))
    result = await pdf.extract_pdf_bytes(b"%PDF-saved-original", python_executable="qualified-python")
    assert result.text == "Full marker output" and result.is_transformation


def test_marker_worker_hard_timeout(monkeypatch):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=1)

    monkeypatch.setattr(pdf.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="timed out"):
        pdf._run_marker_with_hard_timeout("saved.pdf", 1)


def test_marker_worker_uses_configured_python_and_saved_file(monkeypatch):
    def run(cmd, **kwargs):
        assert cmd[0] == "qualified-python" and cmd[cmd.index("--input") + 1] == "saved.pdf"
        Path(cmd[cmd.index("--output") + 1]).write_text("Marker markdown", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pdf.subprocess, "run", run)
    assert pdf._run_marker_with_hard_timeout("saved.pdf", 10, "qualified-python") == "Marker markdown"


def test_marker_worker_error_is_preserved(monkeypatch):
    monkeypatch.setattr(
        pdf.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "marker crashed"),
    )
    with pytest.raises(RuntimeError, match="marker crashed"):
        pdf._run_marker_with_hard_timeout("saved.pdf", 10)


@pytest.mark.asyncio
async def test_fake_pdf_html_and_empty_scans_are_explicit_errors(monkeypatch):
    with pytest.raises(ValueError, match="invalid_pdf_signature"):
        await pdf.extract_pdf_bytes(b"<html>Access denied</html>")
    monkeypatch.setattr(pdf, "_run_marker_with_hard_timeout", lambda *args: "")
    monkeypatch.setattr(pdf, "_fallback_pypdfium2", lambda path: "")
    monkeypatch.setattr(pdf, "_fallback_pypdf2", lambda path: "")
    with pytest.raises(ValueError, match="pdf_extraction_failed"):
        await pdf.extract_pdf_bytes(b"%PDF-scan")
