"""Add a durable fenced worker lease for full V2 runs.

Revision ID: d7a4e2c9b1f6
Revises: c5e8a1d4f2b7
Create Date: 2026-07-14 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7a4e2c9b1f6"
down_revision: Union[str, None] = "c5e8a1d4f2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("generation_runs", sa.Column("v2_worker_task_id", sa.String(length=255), nullable=True))
    op.add_column("generation_runs", sa.Column("v2_worker_claim_token", sa.String(length=36), nullable=True))
    op.add_column("generation_runs", sa.Column("v2_worker_lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("generation_runs", sa.Column("v2_worker_heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("generation_runs", "v2_worker_heartbeat_at")
    op.drop_column("generation_runs", "v2_worker_lease_expires_at")
    op.drop_column("generation_runs", "v2_worker_claim_token")
    op.drop_column("generation_runs", "v2_worker_task_id")
