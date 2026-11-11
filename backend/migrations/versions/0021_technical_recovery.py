"""Linked operational replacements retain ambiguous originals and their reservations."""

from alembic import op
from sqlalchemy import text

revision = "0021_technical_recovery"
down_revision = "0020_search_privacy"
branch_labels = None
depends_on = None


def upgrade():
    previous = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg_get_expr(conbin,conrelid) FROM pg_constraint WHERE conname='evaluation_attempt_shape' AND conrelid='evaluation_model_invocations'::regclass"
            )
        )
        .scalar_one()
    )
    op.drop_constraint("evaluation_attempt_shape", "evaluation_model_invocations", type_="check")
    extra = "(attempt_index>0 AND parent_invocation_id IS NOT NULL AND parent_attempt_index IS NOT NULL AND parent_attempt_index>=0 AND parent_attempt_index<attempt_index AND attempt_kind='technical_retry' AND operation_kind IN ('role','anchor_repair','plan_target_repair'))"
    op.create_check_constraint(
        "evaluation_attempt_shape", "evaluation_model_invocations", "(" + previous + ") OR " + extra
    )


def downgrade():
    raise RuntimeError("Technical recovery provenance must remain addressable.")
