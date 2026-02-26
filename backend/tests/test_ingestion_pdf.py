import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.ingestion import pdf as pdf_module


async def test_convert_with_marker_times_out_and_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pdf_marker_timeout_seconds", 1)
    monkeypatch.setattr(
        pdf_module,
        "_run_marker_with_hard_timeout",
        lambda _pdf_path, _timeout: (_ for _ in ()).throw(TimeoutError("timeout")),
    )
    monkeypatch.setattr(pdf_module, "_fallback_pdf_text", lambda _: "fallback")

    result = await pdf_module._convert_with_marker("dummy.pdf")
    assert result == "fallback"


async def test_convert_with_marker_exception_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pdf_marker_timeout_seconds", 30)
    monkeypatch.setattr(
        pdf_module,
        "_run_marker_with_hard_timeout",
        lambda _pdf_path, _timeout: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(pdf_module, "_fallback_pdf_text", lambda _: "fallback-exception")

    result = await pdf_module._convert_with_marker("dummy.pdf")
    assert result == "fallback-exception"


async def test_convert_with_marker_success(monkeypatch) -> None:
    monkeypatch.setattr(settings, "pdf_marker_timeout_seconds", 30)
    monkeypatch.setattr(pdf_module, "_run_marker_with_hard_timeout", lambda _pdf_path, _timeout: "marker-ok")

    result = await pdf_module._convert_with_marker("dummy.pdf")
    assert result == "marker-ok"


def test_run_marker_with_hard_timeout_raises_timeout(monkeypatch) -> None:
    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=1)

    monkeypatch.setattr(pdf_module.subprocess, "run", _fake_run)

    with pytest.raises(TimeoutError, match="timed out"):
        pdf_module._run_marker_with_hard_timeout("dummy.pdf", 1)


def test_run_marker_with_hard_timeout_returns_markdown(monkeypatch) -> None:
    def _fake_run(cmd, **kwargs):
        output_path = cmd[cmd.index("--output") + 1]
        Path(output_path).write_text("marker markdown", encoding="utf-8")

        class _Done:
            returncode = 0
            stderr = ""
            stdout = ""

        return _Done()

    monkeypatch.setattr(pdf_module.subprocess, "run", _fake_run)

    result = pdf_module._run_marker_with_hard_timeout("dummy.pdf", 10)
    assert result == "marker markdown"


def test_run_marker_with_hard_timeout_raises_on_failed_subprocess(monkeypatch) -> None:
    class _Done:
        returncode = 1
        stderr = "marker crashed"
        stdout = ""

    monkeypatch.setattr(pdf_module.subprocess, "run", lambda *args, **kwargs: _Done())

    with pytest.raises(RuntimeError, match="marker crashed"):
        pdf_module._run_marker_with_hard_timeout("dummy.pdf", 10)


def test_surya_ocr_config_compat_patch_handles_empty_init() -> None:
    # Reproduces transformers+surya config repr path where empty ctor may be used.
    from surya.recognition.model.config import SuryaOCRConfig

    pdf_module._apply_surya_transformers_compat()
    cfg = SuryaOCRConfig()

    assert cfg.encoder is not None
    assert cfg.decoder is not None


def test_surya_ocr_config_compat_patch_resolves_text_config_ambiguity() -> None:
    from surya.recognition.model.config import SuryaOCRConfig, SuryaOCRTextEncoderConfig

    pdf_module._apply_surya_transformers_compat()
    cfg = SuryaOCRConfig()
    cfg.text_encoder = SuryaOCRTextEncoderConfig()

    text_cfg = cfg.get_text_config()
    assert text_cfg == cfg.decoder


def test_surya_table_rec_config_compat_patch_resolves_text_config_ambiguity() -> None:
    from surya.table_rec.model.config import SuryaTableRecConfig

    pdf_module._apply_surya_transformers_compat()
    cfg = SuryaTableRecConfig()
    cfg.text_encoder = object()

    text_cfg = cfg.get_text_config()
    assert text_cfg == cfg.decoder


def test_surya_s3_retry_patch_keeps_remote_path_on_retry(monkeypatch, tmp_path) -> None:
    import surya.common.s3 as surya_s3

    pdf_module._apply_surya_transformers_compat()

    remote = "table_recognition/2025_02_18"
    calls: list[tuple[str, str]] = []
    attempt = {"n": 0}

    def _fake_download_directory(remote_path: str, local_path: str) -> None:
        calls.append((remote_path, local_path))
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise RuntimeError("connection reset")

    monkeypatch.setattr(surya_s3, "download_directory", _fake_download_directory)
    monkeypatch.setattr(surya_s3, "user_cache_dir", lambda _: str(tmp_path))
    monkeypatch.setattr(pdf_module.time, "sleep", lambda _: None)

    class _Base:
        @classmethod
        def from_pretrained(cls, model_path, *args, **kwargs):
            return model_path

    class _DummyConfig(surya_s3.S3DownloaderMixin, _Base):
        pass

    resolved = _DummyConfig.from_pretrained(f"s3://{remote}")
    expected_local = str(Path(tmp_path) / "models" / remote)

    assert resolved == expected_local
    assert len(calls) == 2
    assert calls[0][0] == remote
    assert calls[1][0] == remote
