import uuid
from datetime import datetime, date
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Enum as SAEnum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ClaimType(StrEnum):
    FACT = "fact"
    OPINION = "opinion"
    REPORT = "report"


class VerificationLevel(StrEnum):
    OBSERVED = "observed"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    UNVERIFIED = "unverified"


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_type: Mapped[str] = mapped_column(
        SAEnum(ClaimType, name="claim_type", create_constraint=False, native_enum=False, length=20),
        nullable=False,
    )
    verification_level: Mapped[str] = mapped_column(
        SAEnum(VerificationLevel, name="verification_level", create_constraint=False, native_enum=False, length=20),
        default=VerificationLevel.UNVERIFIED,
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    attribution: Mapped[dict | None] = mapped_column(JSONB)
    as_of_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (
        CheckConstraint(
            "NOT (supports_fact AND contradicts_fact)",
            name="ck_claim_evidence_supports_vs_contradicts",
        ),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("evidence_spans.id", ondelete="CASCADE"), primary_key=True)
    supports_fact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contradicts_fact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    illustrative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
