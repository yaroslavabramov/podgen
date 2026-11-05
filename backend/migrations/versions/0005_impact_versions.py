"""Separate resolved historical quality impacts from newly arriving corrections."""

from alembic import op

revision = "0005_impact_versions"
down_revision = "0004_final_result_unique"
branch_labels = None
depends_on = None


def upgrade():
    # Existing ledger entries predate this migration and every future revalidation.
    op.execute("ALTER TABLE invalidations ADD COLUMN validity_version bigint NOT NULL DEFAULT 0")


def downgrade():
    raise RuntimeError("Dropping quality impact ordering would weaken historical revalidation.")
