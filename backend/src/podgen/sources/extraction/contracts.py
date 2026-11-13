from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    name: str
    version: str
    title: str | None = None
    published_date: str | None = None
    coverage: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    is_transformation: bool = False
