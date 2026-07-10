from __future__ import annotations

from app.generation_v2.contracts.source import CorpusGapKind, SeverityLevel
from app.generation_v2.schemas import CorpusGap

EDITORIAL_FUNCTION_MARKERS = {
    "tension": ["tension", "conflict", "contradiction", "debate"],
    "mechanism": ["mechanism", "how", "process", "system", "why"],
    "context": ["context", "background", "history"],
    "change": ["change", "trend", "shift", "timeline"],
    "impact": ["impact", "stakes", "effect", "consequence"],
    "driver": ["driver", "cause", "incentive"],
    "exception": ["exception", "caveat", "limit"],
}


def add_corpus_gap(
    gaps: list[CorpusGap],
    *,
    trigger_phase: str,
    kind: CorpusGapKind,
    description: str,
    functions: list[str] | None = None,
    source_ids: list[str] | None = None,
    severity: SeverityLevel = SeverityLevel.MEDIUM,
    origin: str = "",
) -> None:
    clean = " ".join(str(description or "").split())
    if not clean:
        return
    gaps.append(
        CorpusGap(
            gap_id=f"{trigger_phase}-{len(gaps) + 1}",
            kind=kind,
            description=clean,
            required_editorial_functions=functions or [],
            related_source_ids=source_ids or [],
            severity=severity,
            origin=origin or trigger_phase,
        )
    )


def functions_from_text(text: str) -> list[str]:
    lowered = str(text or "").lower()
    functions = [
        function
        for function, tokens in EDITORIAL_FUNCTION_MARKERS.items()
        if any(token in lowered for token in tokens)
    ]
    return functions or ["context"]


_functions_from_text = functions_from_text

__all__ = ["EDITORIAL_FUNCTION_MARKERS", "_functions_from_text", "add_corpus_gap", "functions_from_text"]
