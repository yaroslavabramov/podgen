"""Initial schema

Revision ID: 81f9372b1745
Revises:
Create Date: 2026-02-23 20:15:50.486307

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '81f9372b1745'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Independent tables (no FK dependencies)
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('avatar_url', sa.String(length=512), nullable=True),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('provider_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    op.create_table('projects',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_projects_user_id'), 'projects', ['user_id'], unique=False)

    # 2. generation_runs (depends on projects, users; show_bible FK added later to break cycle)
    op.create_table('generation_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('show_bible_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', name='generation_status', native_enum=False, length=20), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('current_phase', sa.String(length=50), nullable=True),
    sa.Column('current_step', sa.String(length=100), nullable=True),
    sa.Column('progress_pct', sa.Float(), nullable=True),
    sa.Column('langgraph_thread_id', sa.String(length=100), nullable=True),
    sa.Column('estimated_cost', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('actual_cost', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_generation_runs_project_id'), 'generation_runs', ['project_id'], unique=False)

    # 3. show_bibles (depends on projects, generation_runs)
    op.create_table('show_bibles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('audience_profile', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tone_and_mood', sa.String(length=50), nullable=False),
    sa.Column('show_style', sa.String(length=50), nullable=False),
    sa.Column('hosts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('voice_profiles', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('forbidden_phrases', postgresql.ARRAY(sa.String()), nullable=False),
    sa.Column('rigor_policy', sa.Text(), nullable=True),
    sa.Column('adjacency_policy', sa.Text(), nullable=True),
    sa.Column('explanation_policy', sa.Text(), nullable=True),
    sa.Column('format_rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('generation_run_id', sa.UUID(), nullable=True),
    sa.Column('is_preset', sa.Boolean(), nullable=False),
    sa.Column('preset_name', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['generation_run_id'], ['generation_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_show_bibles_project_id'), 'show_bibles', ['project_id'], unique=False)

    # 4. Add deferred FK: generation_runs.show_bible_id -> show_bibles.id
    op.create_foreign_key('fk_generation_runs_show_bible_id', 'generation_runs', 'show_bibles', ['show_bible_id'], ['id'])

    # 5. Tables depending on generation_runs
    op.create_table('episode_manifests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('generation_run_id', sa.UUID(), nullable=False),
    sa.Column('research_tree_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('duration_fill_layers', postgresql.ARRAY(sa.String()), nullable=False),
    sa.Column('act_count', sa.Integer(), nullable=True),
    sa.Column('acts_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['generation_run_id'], ['generation_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('generation_run_id')
    )

    op.create_table('script_segments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('generation_run_id', sa.UUID(), nullable=False),
    sa.Column('act_number', sa.Integer(), nullable=False),
    sa.Column('scene_number', sa.Integer(), nullable=False),
    sa.Column('dialogue_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('engagement_score', sa.Float(), nullable=True),
    sa.Column('audit_status', sa.Enum('PENDING', 'PASSED', 'FAILED', 'REVISED', name='audit_status', native_enum=False, length=20), nullable=False),
    sa.Column('revision_count', sa.Integer(), nullable=False),
    sa.Column('quality_metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['generation_run_id'], ['generation_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_script_segments_generation_run_id'), 'script_segments', ['generation_run_id'], unique=False)

    # 6. Tables depending on projects
    op.create_table('claims',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('claim_type', sa.Enum('FACT', 'OPINION', 'REPORT', name='claim_type', native_enum=False, length=20), nullable=False),
    sa.Column('verification_level', sa.Enum('OBSERVED', 'VERIFIED', 'DISPUTED', 'UNVERIFIED', name='verification_level', native_enum=False, length=20), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('attribution', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('as_of_date', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_claims_project_id'), 'claims', ['project_id'], unique=False)

    op.create_table('conflict_briefs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('claim_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
    sa.Column('what_conflicts', sa.Text(), nullable=False),
    sa.Column('why_reason', sa.Text(), nullable=False),
    sa.Column('how_to_present', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conflict_briefs_project_id'), 'conflict_briefs', ['project_id'], unique=False)

    op.create_table('content_maps',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('map_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_content_maps_project_id'), 'content_maps', ['project_id'], unique=False)

    op.create_table('episodes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('generation_run_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('language', sa.String(length=5), nullable=False),
    sa.Column('duration_seconds', sa.Integer(), nullable=False),
    sa.Column('audio_file_path', sa.String(length=512), nullable=True),
    sa.Column('transcript_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('shownotes_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('quality_metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['generation_run_id'], ['generation_runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('generation_run_id')
    )
    op.create_index(op.f('ix_episodes_project_id'), 'episodes', ['project_id'], unique=False)

    op.create_table('sources',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('source_type', sa.Enum('PDF', 'WEB', 'VIDEO', 'AUDIO', name='source_type', native_enum=False, length=20), nullable=False),
    sa.Column('original_url', sa.String(length=2048), nullable=True),
    sa.Column('file_path', sa.String(length=512), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('domain_reliability', sa.Enum('HIGH', 'MEDIUM', 'LOW', 'UNKNOWN', name='domain_reliability', native_enum=False, length=20), nullable=False),
    sa.Column('ingestion_status', sa.Enum('PENDING', 'ENQUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', name='ingestion_status', native_enum=False, length=20), nullable=False),
    sa.Column('is_temporary', sa.Boolean(), nullable=False),
    sa.Column('fully_ingested', sa.Boolean(), nullable=False),
    sa.Column('raw_text_path', sa.String(length=512), nullable=True),
    sa.Column('text_preview', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('generation_run_id', sa.UUID(), nullable=True),
    sa.Column('is_duplicate_of', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('status_changed_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['generation_run_id'], ['generation_runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['is_duplicate_of'], ['sources.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sources_project_id'), 'sources', ['project_id'], unique=False)

    # 7. Tables depending on sources
    op.create_table('chunks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('parent_chunk_id', sa.UUID(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('heading_path', postgresql.ARRAY(sa.String()), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=False),
    sa.Column('qdrant_point_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['parent_chunk_id'], ['chunks.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chunks_source_id'), 'chunks', ['source_id'], unique=False)

    # 8. Tables depending on chunks + sources + projects
    op.create_table('evidence_spans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('chunk_id', sa.UUID(), nullable=True),
    sa.Column('verbatim_quote', sa.Text(), nullable=False),
    sa.Column('locator', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('start_idx', sa.Integer(), nullable=True),
    sa.Column('end_idx', sa.Integer(), nullable=True),
    sa.Column('reliability_hint', sa.String(length=50), nullable=True),
    sa.Column('asset_type', sa.Enum('STATISTIC', 'ANECDOTE', 'EXPERT_QUOTE', 'DEFINITION', 'MECHANISM', 'CASE', 'TIMELINE', 'CONTRAST', name='asset_type', native_enum=False, length=30), nullable=False),
    sa.Column('narrative_value_score', sa.Float(), nullable=True),
    sa.Column('tellability_notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['chunk_id'], ['chunks.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_evidence_spans_project_id'), 'evidence_spans', ['project_id'], unique=False)
    op.create_index(op.f('ix_evidence_spans_source_id'), 'evidence_spans', ['source_id'], unique=False)

    # 9. Join tables
    op.create_table('claim_evidence',
    sa.Column('claim_id', sa.UUID(), nullable=False),
    sa.Column('evidence_id', sa.UUID(), nullable=False),
    sa.Column('relation', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['claim_id'], ['claims.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['evidence_id'], ['evidence_spans.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('claim_id', 'evidence_id')
    )


def downgrade() -> None:
    op.drop_table('claim_evidence')
    op.drop_index(op.f('ix_evidence_spans_source_id'), table_name='evidence_spans')
    op.drop_index(op.f('ix_evidence_spans_project_id'), table_name='evidence_spans')
    op.drop_table('evidence_spans')
    op.drop_index(op.f('ix_chunks_source_id'), table_name='chunks')
    op.drop_table('chunks')
    op.drop_index(op.f('ix_sources_project_id'), table_name='sources')
    op.drop_table('sources')
    op.drop_index(op.f('ix_episodes_project_id'), table_name='episodes')
    op.drop_table('episodes')
    op.drop_index(op.f('ix_content_maps_project_id'), table_name='content_maps')
    op.drop_table('content_maps')
    op.drop_index(op.f('ix_conflict_briefs_project_id'), table_name='conflict_briefs')
    op.drop_table('conflict_briefs')
    op.drop_index(op.f('ix_claims_project_id'), table_name='claims')
    op.drop_table('claims')
    op.drop_index(op.f('ix_script_segments_generation_run_id'), table_name='script_segments')
    op.drop_table('script_segments')
    op.drop_table('episode_manifests')
    op.drop_foreign_key('fk_generation_runs_show_bible_id', 'generation_runs')
    op.drop_index(op.f('ix_show_bibles_project_id'), table_name='show_bibles')
    op.drop_table('show_bibles')
    op.drop_index(op.f('ix_generation_runs_project_id'), table_name='generation_runs')
    op.drop_table('generation_runs')
    op.drop_index(op.f('ix_projects_user_id'), table_name='projects')
    op.drop_table('projects')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
