from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump

logger = logging.getLogger(__name__)


def dump_v2_debug(
    *,
    run_id: str,
    phase: str,
    agent_name: str,
    dump_type: str,
    payload: dict[str, Any],
) -> None:
    dump_dir = (settings.generation_v2_debug_dump_dir or settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    filename_stem = "_".join(
        safe_filename_token(part)
        for part in (f"run-{run_id}", phase, agent_name, dump_type)
        if part
    )
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir=f"generation_v2/{safe_filename_token(phase)}",
        filename_stem=filename_stem,
        payload=payload,
    )
