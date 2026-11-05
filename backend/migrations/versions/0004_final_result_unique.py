"""One immutable semantic result per task run, including partial and failed outcomes."""

from alembic import op

revision = "0004_final_result_unique"
down_revision = "0003_full_unicode_json"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE UNIQUE INDEX one_final_result_per_task_run ON operational_records(project_id,parent_record_id) WHERE record_kind='task_result'"
    )


def downgrade():
    raise RuntimeError("Final result identity cannot be weakened for historical runs.")
