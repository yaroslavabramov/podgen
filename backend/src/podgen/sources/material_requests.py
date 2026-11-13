"""Shared synchronous boundary between durable model tools and asynchronous acquisition."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.persistence.text_source import VerifiedText


@dataclass(frozen=True)
class SourceAdmission:
    source_id: str
    title: str
    text: str | VerifiedText
    sha256: str
    reference: ArtifactRef | RuntimeRef
    catalog: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MaterialResolution:
    # Model-visible result excludes raw document text. Full text is admitted separately.
    result: dict
    admissions: tuple[SourceAdmission, ...] = ()


class MaterialResolver(Protocol):
    def restore(self, *, run_id: UUID, guard: Callable[[], None]) -> tuple[SourceAdmission, ...]: ...

    def resolve(
        self,
        name: str,
        arguments: dict,
        *,
        operation_id: UUID,
        run_id: UUID,
        guard: Callable[[], None],
    ) -> MaterialResolution: ...
