"""Independent readings and decisions, bound to at most three complete script versions."""

from alembic import op

revision = "0015_independent_readings"
down_revision = "0014_plan_target_repair"
branch_labels = None
depends_on = None


def upgrade():
    stages = ["draft", "review", "revise", "plan", "plan-review", "plan-revise", "plan-review-revised"]
    for index in range(3):
        suffix = f"-{index}" if index else ""
        stages.extend(base + suffix for base in ("reader-review", "fact-review"))
    stages.extend(("revise-1", "revise-2"))
    op.drop_constraint(
        "evaluation_model_invocations_stage_check", "evaluation_model_invocations", type_="check"
    )
    op.create_check_constraint(
        "evaluation_model_invocations_stage_check",
        "evaluation_model_invocations",
        "stage IN (" + ",".join("'" + stage + "'" for stage in stages) + ")",
    )

    reading_sql = (
        "stage IN ("
        + ",".join(
            "'" + stage + "'"
            for stage in stages
            if stage == "review" or "-review" in stage and not stage.startswith("plan")
        )
        + ")"
    )
    op.drop_constraint("evaluation_attempt_shape", "evaluation_model_invocations", type_="check")
    op.create_check_constraint(
        "evaluation_attempt_shape",
        "evaluation_model_invocations",
        f"""
      (attempt_index=0 AND attempt_kind='role' AND operation_kind='role' AND parent_invocation_id IS NULL AND parent_attempt_index IS NULL)
      OR (attempt_index>0 AND parent_invocation_id IS NOT NULL AND parent_attempt_index IS NOT NULL AND parent_attempt_index>=0 AND parent_attempt_index<attempt_index
        AND ((attempt_kind='anchor_repair' AND operation_kind='anchor_repair' AND {reading_sql})
          OR (attempt_kind='plan_target_repair' AND operation_kind='plan_target_repair' AND stage IN ('plan-review','plan-review-revised'))
          OR (attempt_kind='credit_retry' AND (operation_kind='role' OR (operation_kind='anchor_repair' AND {reading_sql})
               OR (operation_kind='plan_target_repair' AND stage IN ('plan-review','plan-review-revised'))))))
    """,
    )


def downgrade():
    raise RuntimeError("Independent readings and script version decisions must remain addressable.")
