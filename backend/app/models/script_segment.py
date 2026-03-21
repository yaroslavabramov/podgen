import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Integer, Float, DateTime, ForeignKey, Enum as SAEnum, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    REVISED = "revised"


class ScriptSegment(Base):
    __tablename__ = "script_segments"
    __table_args__ = (
        UniqueConstraint(
            "generation_run_id",
            "act_number",
            "scene_number",
            name="uq_script_segments_run_act_scene",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("generation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    act_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dialogue_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    engagement_score: Mapped[float | None] = mapped_column(Float)
    audit_status: Mapped[str] = mapped_column(
        SAEnum(AuditStatus, name="audit_status", create_constraint=False, native_enum=False, length=20),
        default=AuditStatus.PENDING,
    )
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_metrics: Mapped[dict | None] = mapped_column(JSONB)
    audit_findings: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
