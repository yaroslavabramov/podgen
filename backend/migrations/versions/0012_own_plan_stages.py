"""Add bounded planning stages without changing any existing run or attempt identity."""

from alembic import op

revision = "0012_own_plan_stages"
down_revision = "0011_review_anchor_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "evaluation_model_invocations_stage_check", "evaluation_model_invocations", type_="check"
    )
    op.create_check_constraint(
        "evaluation_model_invocations_stage_check",
        "evaluation_model_invocations",
        "stage IN ('draft','review','revise','plan','plan-review','plan-revise','plan-review-revised')",
    )


def downgrade():
    raise RuntimeError("Planning candidates and decisions must remain addressable.")
