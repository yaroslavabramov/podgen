"""One explicit address-only repair per planning review, with credit-retry continuity."""

from alembic import op

revision = "0014_plan_target_repair"
down_revision = "0013_credit_refusal_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("evaluation_attempt_shape", "evaluation_model_invocations", type_="check")
    op.create_check_constraint(
        "evaluation_attempt_shape",
        "evaluation_model_invocations",
        """
      (attempt_index=0 AND attempt_kind='role' AND operation_kind='role' AND parent_invocation_id IS NULL AND parent_attempt_index IS NULL)
      OR (attempt_index>0 AND parent_invocation_id IS NOT NULL AND parent_attempt_index IS NOT NULL AND parent_attempt_index>=0 AND parent_attempt_index<attempt_index
        AND ((attempt_kind='anchor_repair' AND operation_kind='anchor_repair' AND stage='review')
          OR (attempt_kind='plan_target_repair' AND operation_kind='plan_target_repair' AND stage IN ('plan-review','plan-review-revised'))
          OR (attempt_kind='credit_retry' AND (operation_kind='role' OR (operation_kind='anchor_repair' AND stage='review')
               OR (operation_kind='plan_target_repair' AND stage IN ('plan-review','plan-review-revised'))))))
    """,
    )
    op.execute(
        "CREATE UNIQUE INDEX evaluation_one_plan_target_repair ON evaluation_model_invocations(project_id,run_id,stage) WHERE attempt_kind='plan_target_repair'"
    )


def downgrade():
    raise RuntimeError("Address repair attempts must remain archived.")
