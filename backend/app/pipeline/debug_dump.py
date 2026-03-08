"""Shared helpers for writing pipeline debug dumps."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[3]


def safe_filename_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    token = token.strip("-_")
    return token or "unknown"


def resolve_dump_dir(raw_dir: str) -> Path:
    path = Path(raw_dir).expanduser()
    if path.is_absolute():
        return path
    return _REPO_ROOT / path


def write_json_debug_dump(
    *,
    raw_dir: str,
    subdir: str,
    filename_stem: str,
    payload: dict,
) -> None:
    if not raw_dir.strip():
        return

    try:
        target_dir = resolve_dump_dir(raw_dir) / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        payload_hash = hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:10]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{ts}_{filename_stem}_{payload_hash}.json"
        (target_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Failed to write pipeline debug dump for %s/%s", subdir, filename_stem, exc_info=True)
