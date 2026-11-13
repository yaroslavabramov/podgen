"""Qualified legacy compatibility for Surya/transformers and stable model paths."""

import os
import time
from pathlib import Path


def _apply_surya_transformers_compat() -> None:
    """Patch Surya OCR config init for newer transformers compatibility.

    surya-ocr 0.12.x defines `SuryaOCRConfig.__init__` with mandatory
    `kwargs.pop("encoder")` / `kwargs.pop("decoder")`. Newer transformers call
    `ConfigClass()` during config repr/logging, which raises KeyError and breaks
    marker model initialization.
    """
    try:
        import surya.common.s3 as surya_s3
        from surya.layout.model.config import SuryaLayoutConfig
        from surya.recognition.model.config import DonutSwinConfig, SuryaOCRConfig, SuryaOCRDecoderConfig
        from surya.table_rec.model.config import SuryaTableRecConfig
        from surya.texify.model.config import TexifyConfig
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
        if not isinstance(pretrained_model_name_or_path, str) or not pretrained_model_name_or_path.startswith(
            "s3://"
        ):
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
