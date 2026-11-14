"""Immutable operation results, separate from fresh read/dispatch authorization."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    key: str
    prepared: dict
    audio: dict | None = None
    decoded: dict | None = None
    result: dict | None = None
    failure: dict | None = None


@dataclass(frozen=True)
class SampleResult:
    status: str
    cell_id: str
    result_ref: dict | None
    baseline: dict | None
    attempts_submitted: int
    committed_usd: Decimal
