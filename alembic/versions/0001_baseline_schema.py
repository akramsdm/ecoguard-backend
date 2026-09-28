from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0001_baseline_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('roles', sa.JSON(), nullable=False),
        sa.Column('areas', sa.JSON(), nullable=False),
        sa.Column('preferences', sa.JSON(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    op.create_table(
        'areas',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=False),
        sa.Column('longitude', sa.Float(), nullable=False),
        sa.Column('radius_km', sa.Float(), nullable=False),
    )

    op.create_table(
        'outbox',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('aggregate_id', sa.String(length=36), nullable=False),
        sa.Column('dedupe_key', sa.String(length=100), nullable=False, unique=True),
        sa.Column('state', sa.String(length=24), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('updated_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_outbox_state', 'outbox', ['state'], unique=False)

    op.create_table(
        'sessions',
        sa.Column('token_hash', sa.String(length=64), primary_key=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('csrf', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
    )
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'], unique=False)

    op.create_table(
        'reports',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('code', sa.String(length=24), nullable=False),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('area_id', sa.String(length=36), sa.ForeignKey('areas.id'), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('species', sa.String(length=100), nullable=False),
        sa.Column('observed_at', sa.String(length=40), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('share_location', sa.Boolean(), nullable=False),
        sa.Column('consent', sa.Boolean(), nullable=False),
        sa.Column('state', sa.String(length=24), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('assignee_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('updated_at', sa.String(length=40), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.UniqueConstraint('owner_id', 'client_id', name='uq_report_client'),
    )
    op.create_index('ix_reports_code', 'reports', ['code'], unique=True)
    op.create_index('ix_reports_owner_id', 'reports', ['owner_id'], unique=False)
    op.create_index('ix_reports_area_id', 'reports', ['area_id'], unique=False)
    op.create_index('ix_reports_category', 'reports', ['category'], unique=False)
    op.create_index('ix_reports_state', 'reports', ['state'], unique=False)
    op.create_index('ix_reports_created_at', 'reports', ['created_at'], unique=False)

    op.create_table(
        'evidence',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('owner_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=True),
        sa.Column('storage_key', sa.String(length=100), nullable=False, unique=True),
        sa.Column('filename', sa.String(length=100), nullable=False),
        sa.Column('content_type', sa.String(length=30), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_evidence_owner_id', 'evidence', ['owner_id'], unique=False)
    op.create_index('ix_evidence_report_id', 'evidence', ['report_id'], unique=False)

    op.create_table(
        'predictions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('evidence_id', sa.String(length=36), sa.ForeignKey('evidence.id'), nullable=False),
        sa.Column('state', sa.String(length=24), nullable=False),
        sa.Column('species', sa.String(length=100), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('boxes', sa.JSON(), nullable=False),
        sa.Column('model_version', sa.String(length=100), nullable=False),
        sa.Column('explanation', sa.Text(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_predictions_evidence_id', 'predictions', ['evidence_id'], unique=False)

    op.create_table(
        'reviews',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('reviewer_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('decision', sa.String(length=24), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('species', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_reviews_report_id', 'reviews', ['report_id'], unique=False)

    op.create_table(
        'case_events',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('actor_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_case_events_report_id', 'case_events', ['report_id'], unique=False)

    op.create_table(
        'advisories',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('area_id', sa.String(length=36), sa.ForeignKey('areas.id'), nullable=False),
        sa.Column('author_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('publisher_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('category', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=250), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('expires_at', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('published_at', sa.String(length=40), nullable=True),
        sa.Column('retraction_reason', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
    )
    op.create_index('ix_advisories_report_id', 'advisories', ['report_id'], unique=False)
    op.create_index('ix_advisories_area_id', 'advisories', ['area_id'], unique=False)
    op.create_index('ix_advisories_state', 'advisories', ['state'], unique=False)

    op.create_table(
        'receipts',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('user_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('advisory_id', sa.String(length=36), sa.ForeignKey('advisories.id'), nullable=False),
        sa.Column('read_at', sa.String(length=40), nullable=False),
        sa.UniqueConstraint('user_id', 'advisory_id', name='uq_receipt'),
    )
    op.create_index('ix_receipts_user_id', 'receipts', ['user_id'], unique=False)
    op.create_index('ix_receipts_advisory_id', 'receipts', ['advisory_id'], unique=False)

    op.create_table(
        'messages',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('report_id', sa.String(length=36), sa.ForeignKey('reports.id'), nullable=False),
        sa.Column('sender_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )
    op.create_index('ix_messages_report_id', 'messages', ['report_id'], unique=False)

    op.create_table(
        'audit',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('actor_id', sa.String(length=36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('action', sa.String(length=80), nullable=False),
        sa.Column('target_id', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.String(length=40), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('audit')

    op.drop_index('ix_messages_report_id', table_name='messages')
    op.drop_table('messages')

    op.drop_index('ix_receipts_advisory_id', table_name='receipts')
    op.drop_index('ix_receipts_user_id', table_name='receipts')
    op.drop_table('receipts')

    op.drop_index('ix_advisories_state', table_name='advisories')
    op.drop_index('ix_advisories_area_id', table_name='advisories')
    op.drop_index('ix_advisories_report_id', table_name='advisories')
    op.drop_table('advisories')

    op.drop_index('ix_case_events_report_id', table_name='case_events')
    op.drop_table('case_events')

    op.drop_index('ix_reviews_report_id', table_name='reviews')
    op.drop_table('reviews')

    op.drop_index('ix_predictions_evidence_id', table_name='predictions')
    op.drop_table('predictions')

    op.drop_index('ix_evidence_report_id', table_name='evidence')
    op.drop_index('ix_evidence_owner_id', table_name='evidence')
    op.drop_table('evidence')

    op.drop_index('ix_reports_created_at', table_name='reports')
    op.drop_index('ix_reports_state', table_name='reports')
    op.drop_index('ix_reports_category', table_name='reports')
    op.drop_index('ix_reports_area_id', table_name='reports')
    op.drop_index('ix_reports_owner_id', table_name='reports')
    op.drop_index('ix_reports_code', table_name='reports')
    op.drop_table('reports')

    op.drop_index('ix_sessions_user_id', table_name='sessions')
    op.drop_table('sessions')

    op.drop_table('outbox')
    op.drop_table('areas')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')