"""Durable relational identities and versioned JSON evidence payloads."""
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Candidate(Record, Base):
    __tablename__ = "candidates"
    name: Mapped[str] = mapped_column(String(250), default="Owner")
    revision: Mapped[str] = mapped_column(String(64), default="initial")


class ProfileFact(Record, Base):
    __tablename__ = "profile_facts"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), default="owner")
    key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="unconfirmed")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ProfileRevision(Record, Base):
    __tablename__ = "profile_revisions"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), default="owner")


class Answer(Record, Base):
    __tablename__ = "answers"
    key: Mapped[str] = mapped_column(String(160))
    context: Mapped[str] = mapped_column(String(200), default="global")
    __table_args__ = (UniqueConstraint("key", "context"),)


class PolicyRevision(Record, Base):
    __tablename__ = "policy_revisions"


class Control(Record, Base):
    __tablename__ = "controls"


class Source(Record, Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(250))
    connector: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="never_checked")


class SourceRun(Record, Base):
    __tablename__ = "source_runs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))


class Opportunity(Record, Base):
    __tablename__ = "opportunities"
    canonical_key: Mapped[str] = mapped_column(String(600), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    employer: Mapped[str] = mapped_column(String(250))
    country: Mapped[str] = mapped_column(String(10), default="")
    track: Mapped[str] = mapped_column(String(50), default="data_ai")
    route: Mapped[str] = mapped_column(String(40), default="manual")
    url: Mapped[str] = mapped_column(Text)
    availability: Mapped[str] = mapped_column(String(24), default="unverified")
    content_hash: Mapped[str] = mapped_column(String(64))


class SourceOccurrence(Record, Base):
    __tablename__ = "source_occurrences"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    occurrence_key: Mapped[str] = mapped_column(String(700), unique=True)


class Requirement(Record, Base):
    __tablename__ = "requirements"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)


class Assessment(Record, Base):
    __tablename__ = "assessments"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    profile_revision: Mapped[str] = mapped_column(String(64))
    posting_hash: Mapped[str] = mapped_column(String(64))


class Application(Record, Base):
    __tablename__ = "applications"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), default="owner")
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), index=True)
    state: Mapped[str] = mapped_column(String(40), default="discovered", index=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("candidate_id", "opportunity_id"),)


class Packet(Record, Base):
    __tablename__ = "packets"
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")


class DocumentArtifact(Record, Base):
    __tablename__ = "document_artifacts"
    packet_id: Mapped[str | None] = mapped_column(ForeignKey("packets.id"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)


class SubmissionAttempt(Record, Base):
    __tablename__ = "submission_attempts"
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(200), unique=True)


class OutboxEvent(Record, Base):
    __tablename__ = "outbox_events"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    kind: Mapped[str] = mapped_column(String(40), default="submit")


class EmailMessage(Record, Base):
    __tablename__ = "email_messages"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    external_id: Mapped[str] = mapped_column(String(300), unique=True)


class HumanTask(Record, Base):
    __tablename__ = "human_tasks"
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    group_key: Mapped[str] = mapped_column(String(300), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)


class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(80))


class BudgetLedger(Record, Base):
    __tablename__ = "budget_ledger"
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(40))
    reservation_key: Mapped[str] = mapped_column(String(200), unique=True)


class Connection(Record, Base):
    __tablename__ = "connections"
    status: Mapped[str] = mapped_column(String(32), default="unconfigured")
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Record, Base):
    __tablename__ = "jobs"
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    due_at: Mapped[str] = mapped_column(String(40), default=now_iso, index=True)
    lease_until: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Notification(Record, Base):
    __tablename__ = "notifications"
    key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="unread")


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    expires_at: Mapped[str] = mapped_column(String(40))


def _immutable_record(mapper, connection, target):
    raise ValueError(f"{type(target).__name__} is append-only")


for _model in (ProfileRevision, PolicyRevision, SourceRun, Assessment, SubmissionAttempt,
               EmailMessage, AuditEvent, BudgetLedger, DocumentArtifact):
    event.listen(_model, "before_update", _immutable_record)
    event.listen(_model, "before_delete", _immutable_record)
