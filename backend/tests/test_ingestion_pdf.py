import queue
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


def test_run_marker_with_hard_timeout_terminates_stuck_process(monkeypatch) -> None:
    class FakeQueue:
        def get_nowait(self):
            raise queue.Empty

        def close(self):
            return None

        def join_thread(self):
            return None

    class FakeProcess:
        def __init__(self):
            self._alive = True
            self.terminate_called = False
            self.kill_called = False
            self.exitcode = None

        def start(self):
            return None

        def join(self, _timeout=None):
            return None

        def is_alive(self):
            return self._alive

        def terminate(self):
            self.terminate_called = True
            self._alive = False
            self.exitcode = -15

        def kill(self):
            self.kill_called = True
            self._alive = False
            self.exitcode = -9

    class FakeContext:
        def __init__(self):
            self.process = FakeProcess()

        def Queue(self, maxsize=1):  # noqa: N802
            return FakeQueue()

        def Process(self, *args, **kwargs):  # noqa: N802
            return self.process

    fake_context = FakeContext()
    monkeypatch.setattr(pdf_module.multiprocessing, "get_context", lambda _name: fake_context)

    with pytest.raises(TimeoutError):
        pdf_module._run_marker_with_hard_timeout("dummy.pdf", 1)

    assert fake_context.process.terminate_called is True


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
