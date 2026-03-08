"""Store asset_type on evidence provenance and key uniqueness on it.

Revision ID: f4d2c1a7b9e3
Revises: c7e1fb1d8f4a
Create Date: 2026-03-07 22:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f4d2c1a7b9e3"
down_revision: Union[str, None] = "c7e1fb1d8f4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_provenance",
        sa.Column(
            "asset_type",
            sa.Enum(
                "STATISTIC",
                "ANECDOTE",
                "EXPERT_QUOTE",
                "DEFINITION",
                "MECHANISM",
                "CASE",
                "TIMELINE",
                "CONTRAST",
                name="asset_type",
                native_enum=False,
                length=30,
            ),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE evidence_provenance ep
        SET asset_type = es.asset_type
        FROM evidence_spans es
        WHERE ep.evidence_id = es.id
        """
    )
    op.alter_column("evidence_provenance", "asset_type", nullable=False)
    op.drop_constraint("uq_evidence_provenance_context", "evidence_provenance", type_="unique")
    op.create_unique_constraint(
        "uq_evidence_provenance_context",
        "evidence_provenance",
        [
            "evidence_id",
            "generation_run_id",
            "question_id",
            "branch_id",
            "retrieved_by",
            "asset_type",
        ],
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM evidence_provenance older
        USING evidence_provenance newer
        WHERE older.id < newer.id
          AND older.evidence_id = newer.evidence_id
          AND older.generation_run_id = newer.generation_run_id
          AND older.question_id = newer.question_id
          AND older.branch_id = newer.branch_id
          AND older.retrieved_by = newer.retrieved_by
        """
    )
    op.drop_constraint("uq_evidence_provenance_context", "evidence_provenance", type_="unique")
    op.create_unique_constraint(
        "uq_evidence_provenance_context",
        "evidence_provenance",
        [
            "evidence_id",
            "generation_run_id",
            "question_id",
            "branch_id",
            "retrieved_by",
        ],
    )
    op.drop_column("evidence_provenance", "asset_type")
