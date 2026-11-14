"""Index immutable elapsed authorizations by their owning run."""

from alembic import op

revision = "0029_elapsed_authorizations"
down_revision = "0028_private_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX runtime_elapsed_authorizations_by_run
        ON runtime_records(project_id,(payload->>'run_id'))
        WHERE record_kind='tool_execution'
          AND payload->>'contract'='resume-elapsed-authorization/1'""")


def downgrade():
    op.execute("DROP INDEX runtime_elapsed_authorizations_by_run")
