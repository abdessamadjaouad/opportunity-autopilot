"""Initial durable workflow schema; frozen 2026-09-09."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('answers',
    sa.Column('key', sa.String(length=160), nullable=False),
    sa.Column('context', sa.String(length=200), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', 'context')
    )
    op.create_index(op.f('ix_answers_created_at'), 'answers', ['created_at'], unique=False)
    op.create_table('audit_events',
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_created_at'), 'audit_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_events_entity_id'), 'audit_events', ['entity_id'], unique=False)
    op.create_table('auth_sessions',
    sa.Column('expires_at', sa.String(length=40), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_auth_sessions_created_at'), 'auth_sessions', ['created_at'], unique=False)
    op.create_table('budget_ledger',
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('reservation_key', sa.String(length=200), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reservation_key')
    )
    op.create_index(op.f('ix_budget_ledger_created_at'), 'budget_ledger', ['created_at'], unique=False)
    op.create_table('candidates',
    sa.Column('name', sa.String(length=250), nullable=False),
    sa.Column('revision', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_candidates_created_at'), 'candidates', ['created_at'], unique=False)
    op.create_table('connections',
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('encrypted_secret', sa.Text(), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_connections_created_at'), 'connections', ['created_at'], unique=False)
    op.create_table('controls',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_controls_created_at'), 'controls', ['created_at'], unique=False)
    op.create_table('jobs',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('due_at', sa.String(length=40), nullable=False),
    sa.Column('lease_until', sa.String(length=40), nullable=True),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_index(op.f('ix_jobs_created_at'), 'jobs', ['created_at'], unique=False)
    op.create_index(op.f('ix_jobs_due_at'), 'jobs', ['due_at'], unique=False)
    op.create_index(op.f('ix_jobs_state'), 'jobs', ['state'], unique=False)
    op.create_table('notifications',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_index(op.f('ix_notifications_created_at'), 'notifications', ['created_at'], unique=False)
    op.create_table('opportunities',
    sa.Column('canonical_key', sa.String(length=600), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('employer', sa.String(length=250), nullable=False),
    sa.Column('country', sa.String(length=10), nullable=False),
    sa.Column('track', sa.String(length=50), nullable=False),
    sa.Column('route', sa.String(length=40), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('availability', sa.String(length=24), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('canonical_key')
    )
    op.create_index(op.f('ix_opportunities_created_at'), 'opportunities', ['created_at'], unique=False)
    op.create_table('policy_revisions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_policy_revisions_created_at'), 'policy_revisions', ['created_at'], unique=False)
    op.create_table('sources',
    sa.Column('name', sa.String(length=250), nullable=False),
    sa.Column('connector', sa.String(length=50), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=40), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sources_created_at'), 'sources', ['created_at'], unique=False)
    op.create_table('applications',
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('opportunity_id', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=40), nullable=False),
    sa.Column('updated_at', sa.String(length=40), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('candidate_id', 'opportunity_id')
    )
    op.create_index(op.f('ix_applications_created_at'), 'applications', ['created_at'], unique=False)
    op.create_index(op.f('ix_applications_opportunity_id'), 'applications', ['opportunity_id'], unique=False)
    op.create_index(op.f('ix_applications_state'), 'applications', ['state'], unique=False)
    op.create_table('assessments',
    sa.Column('opportunity_id', sa.String(length=64), nullable=False),
    sa.Column('profile_revision', sa.String(length=64), nullable=False),
    sa.Column('posting_hash', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assessments_created_at'), 'assessments', ['created_at'], unique=False)
    op.create_index(op.f('ix_assessments_opportunity_id'), 'assessments', ['opportunity_id'], unique=False)
    op.create_table('profile_facts',
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=160), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_index(op.f('ix_profile_facts_created_at'), 'profile_facts', ['created_at'], unique=False)
    op.create_table('profile_revisions',
    sa.Column('candidate_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_profile_revisions_created_at'), 'profile_revisions', ['created_at'], unique=False)
    op.create_table('requirements',
    sa.Column('opportunity_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_requirements_created_at'), 'requirements', ['created_at'], unique=False)
    op.create_index(op.f('ix_requirements_opportunity_id'), 'requirements', ['opportunity_id'], unique=False)
    op.create_table('source_occurrences',
    sa.Column('opportunity_id', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=True),
    sa.Column('occurrence_key', sa.String(length=700), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('occurrence_key')
    )
    op.create_index(op.f('ix_source_occurrences_created_at'), 'source_occurrences', ['created_at'], unique=False)
    op.create_index(op.f('ix_source_occurrences_opportunity_id'), 'source_occurrences', ['opportunity_id'], unique=False)
    op.create_table('source_runs',
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_source_runs_created_at'), 'source_runs', ['created_at'], unique=False)
    op.create_table('email_messages',
    sa.Column('application_id', sa.String(length=64), nullable=True),
    sa.Column('external_id', sa.String(length=300), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('external_id')
    )
    op.create_index(op.f('ix_email_messages_created_at'), 'email_messages', ['created_at'], unique=False)
    op.create_table('human_tasks',
    sa.Column('application_id', sa.String(length=64), nullable=True),
    sa.Column('group_key', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_key')
    )
    op.create_index(op.f('ix_human_tasks_created_at'), 'human_tasks', ['created_at'], unique=False)
    op.create_index(op.f('ix_human_tasks_status'), 'human_tasks', ['status'], unique=False)
    op.create_table('outbox_events',
    sa.Column('application_id', sa.String(length=64), nullable=True),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key')
    )
    op.create_index(op.f('ix_outbox_events_created_at'), 'outbox_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_outbox_events_state'), 'outbox_events', ['state'], unique=False)
    op.create_table('packets',
    sa.Column('application_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_packets_application_id'), 'packets', ['application_id'], unique=False)
    op.create_index(op.f('ix_packets_created_at'), 'packets', ['created_at'], unique=False)
    op.create_table('submission_attempts',
    sa.Column('application_id', sa.String(length=64), nullable=False),
    sa.Column('message_id', sa.String(length=200), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id')
    )
    op.create_index(op.f('ix_submission_attempts_application_id'), 'submission_attempts', ['application_id'], unique=False)
    op.create_index(op.f('ix_submission_attempts_created_at'), 'submission_attempts', ['created_at'], unique=False)
    op.create_table('document_artifacts',
    sa.Column('packet_id', sa.String(length=64), nullable=True),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('mime_type', sa.String(length=100), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['packet_id'], ['packets.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_artifacts_created_at'), 'document_artifacts', ['created_at'], unique=False)
    op.create_index(op.f('ix_document_artifacts_packet_id'), 'document_artifacts', ['packet_id'], unique=False)
    # Enforce evidence immutability even when SQL bypasses the ORM.
    immutable = ("profile_revisions", "policy_revisions", "source_runs", "assessments",
                 "submission_attempts", "email_messages", "audit_events", "budget_ledger", "document_artifacts")
    if op.get_bind().dialect.name == "sqlite":
        for table in immutable:
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'Evidence records are append-only'); END")
    elif op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE FUNCTION reject_immutable_change() RETURNS trigger LANGUAGE plpgsql AS "
                   "$$ BEGIN RAISE EXCEPTION 'Evidence records are append-only'; END $$")
        for table in immutable:
            op.execute(f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION reject_immutable_change()")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup instead")
