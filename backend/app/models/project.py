import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Language(StrEnum):
    EN = "en"
    RU = "ru"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default=Language.EN)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="projects")
    sources: Mapped[list["Source"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    show_bibles: Mapped[list["ShowBible"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    episodes: Mapped[list["Episode"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    generation_runs: Mapped[list["GenerationRun"]] = relationship(back_populates="project", cascade="all, delete-orphan")
