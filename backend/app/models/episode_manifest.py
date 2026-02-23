import uuid

from sqlalchemy import Integer, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EpisodeManifest(Base):
    __tablename__ = "episode_manifests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("generation_runs.id", ondelete="CASCADE"), nullable=False, unique=True)
    research_tree_json: Mapped[dict | None] = mapped_column(JSONB)
    duration_fill_layers: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    act_count: Mapped[int | None] = mapped_column(Integer)
    acts_json: Mapped[list | None] = mapped_column(JSONB)
