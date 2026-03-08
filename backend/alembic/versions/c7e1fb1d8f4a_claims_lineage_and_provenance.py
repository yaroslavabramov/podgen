"""Add claims/evidence lineage, authority, and richer claim-evidence flags.

Revision ID: c7e1fb1d8f4a
Revises: 4a42f5a3614f
Create Date: 2026-03-07 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c7e1fb1d8f4a"
down_revision: Union[str, None] = "4a42f5a3614f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "authority_type",
            sa.Enum(
                "PRIMARY",
                "SECONDARY",
                "TERTIARY",
                "UNKNOWN",
                name="authority_type",
                native_enum=False,
                length=20,
            ),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )
    op.alter_column("sources", "authority_type", server_default=None)

    op.create_table(
        "evidence_provenance",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("generation_run_id", sa.UUID(), nullable=False),
        sa.Column("manifest_id", sa.UUID(), nullable=True),
        sa.Column("question_id", sa.String(length=100), nullable=False),
        sa.Column("branch_id", sa.String(length=100), nullable=False),
        sa.Column(
            "retrieved_by",
            sa.Enum(
                "HUNTER",
                "LIBRARIAN_FAST",
                "DEEP_INGESTION",
                "ACT_PLANNER",
                name="evidence_retrieved_by",
                native_enum=False,
                length=30,
            ),
            nullable=False,
        ),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("retrieval_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence_spans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_run_id"], ["generation_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["manifest_id"], ["episode_manifests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evidence_id",
            "generation_run_id",
            "question_id",
            "branch_id",
            "retrieved_by",
            name="uq_evidence_provenance_context",
        ),
    )
    op.create_index(
        "ix_evidence_provenance_run_branch",
        "evidence_provenance",
        ["generation_run_id", "branch_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_provenance_run_question",
        "evidence_provenance",
        ["generation_run_id", "question_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_provenance_evidence_id",
        "evidence_provenance",
        ["evidence_id"],
        unique=False,
    )

    op.add_column(
        "claim_evidence",
        sa.Column("supports_fact", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "claim_evidence",
        sa.Column("contradicts_fact", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "claim_evidence",
        sa.Column("illustrative", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.execute(
        """
        UPDATE claim_evidence
        SET
            supports_fact = CASE WHEN relation = 'supporting' THEN true ELSE false END,
            contradicts_fact = CASE WHEN relation = 'counter' THEN true ELSE false END,
            illustrative = CASE WHEN relation = 'illustrative' THEN true ELSE false END
        """
    )

    op.alter_column("claim_evidence", "supports_fact", server_default=None)
    op.alter_column("claim_evidence", "contradicts_fact", server_default=None)
    op.alter_column("claim_evidence", "illustrative", server_default=None)
    op.create_check_constraint(
        "ck_claim_evidence_supports_vs_contradicts",
        "claim_evidence",
        "NOT (supports_fact AND contradicts_fact)",
    )
    op.drop_column("claim_evidence", "relation")


def downgrade() -> None:
    op.add_column(
        "claim_evidence",
        sa.Column("relation", sa.String(length=20), nullable=False, server_default="supporting"),
    )
    op.execute(
        """
        UPDATE claim_evidence
        SET relation = CASE
            WHEN contradicts_fact THEN 'counter'
            WHEN illustrative AND NOT supports_fact THEN 'illustrative'
            ELSE 'supporting'
        END
        """
    )
    op.alter_column("claim_evidence", "relation", server_default=None)
    op.drop_constraint("ck_claim_evidence_supports_vs_contradicts", "claim_evidence", type_="check")
    op.drop_column("claim_evidence", "illustrative")
    op.drop_column("claim_evidence", "contradicts_fact")
    op.drop_column("claim_evidence", "supports_fact")

    op.drop_index("ix_evidence_provenance_evidence_id", table_name="evidence_provenance")
    op.drop_index("ix_evidence_provenance_run_question", table_name="evidence_provenance")
    op.drop_index("ix_evidence_provenance_run_branch", table_name="evidence_provenance")
    op.drop_table("evidence_provenance")

    op.drop_column("sources", "authority_type")
