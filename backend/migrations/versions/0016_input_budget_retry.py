"""One explicitly authorized retry of a proven unsent input-budget rejection."""

from alembic import op
from sqlalchemy import text

revision = "0016_input_budget_retry"
down_revision = "0015_independent_readings"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    previous = connection.execute(
        text(
            "SELECT pg_get_expr(conbin,conrelid) FROM pg_constraint WHERE conname='evaluation_attempt_shape' AND conrelid='evaluation_model_invocations'::regclass"
        )
    ).scalar_one()
    op.drop_constraint("evaluation_attempt_shape", "evaluation_model_invocations", type_="check")
    extra = "(attempt_index>0 AND parent_invocation_id IS NOT NULL AND parent_attempt_index IS NOT NULL AND parent_attempt_index>=0 AND parent_attempt_index<attempt_index AND attempt_kind='input_budget_retry' AND operation_kind='role')"
    op.create_check_constraint(
        "evaluation_attempt_shape", "evaluation_model_invocations", "(" + previous + ") OR " + extra
    )


def downgrade():
    raise RuntimeError("Authorized request history must remain addressable.")
