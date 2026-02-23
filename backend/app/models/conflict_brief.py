import uuid

from sqlalchemy import Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConflictBrief(Base):
    __tablename__ = "conflict_briefs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    what_conflicts: Mapped[str] = mapped_column(Text, nullable=False)
    why_reason: Mapped[str] = mapped_column(Text, nullable=False)
    how_to_present: Mapped[str] = mapped_column(Text, nullable=False)
