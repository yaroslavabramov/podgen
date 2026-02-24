"""Enforce unique script segment per run/act/scene.

Revision ID: 9d5a6d6c0f0a
Revises: 81f9372b1745
Create Date: 2026-02-24 03:55:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d5a6d6c0f0a"
down_revision: Union[str, None] = "81f9372b1745"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the newest row per (run, act, scene) before adding uniqueness.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                ctid,
                row_number() OVER (
                    PARTITION BY generation_run_id, act_number, scene_number
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC, id DESC
                ) AS rn
            FROM script_segments
        )
        DELETE FROM script_segments s
        USING ranked r
        WHERE s.ctid = r.ctid
          AND r.rn > 1
        """
    )

    op.create_unique_constraint(
        "uq_script_segments_run_act_scene",
        "script_segments",
        ["generation_run_id", "act_number", "scene_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_script_segments_run_act_scene", "script_segments", type_="unique")
