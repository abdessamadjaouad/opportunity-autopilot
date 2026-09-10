from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.applications.dispatch import dispatch_next, queue_application
from app.applications.followups import dispatch_followup
from app.applications.notifications import dispatch_notification
from app.applications.reconciliation import associate_message, owner_reconciliation, record_message
from app.integrations.simulator import MailSimulator
from app.models import (Answer, Application, AuditEvent, Connection, Notification, OutboxEvent, PolicyRevision)
from app.services import latest_policy
from tests.integration.test_dispatch import ready as dispatch_ready

ready = dispatch_ready


def test_owner_receipt_and_retry_decisions_never_override_known_acceptance(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    with pytest.raises(ValueError, match="Known provider acceptance"):
        owner_reconciliation(db, identity, "authorize_retry", "Owner inspected the sent folder", True)
    result = owner_reconciliation(db, identity, "confirm_receipt", "Employer portal receipt FIXTURE-123 received today")
    assert result["state"] == "submitted_confirmed"
    assert result["confirmation_kind"] == "owner_reported_receipt"
    assert len(provider.sent) == 1


def test_explicit_uncertain_retry_reassesses_instead_of_sending(db, settings, ready):
    identity, provider, _ = ready
    provider.outcome = "timeout_after_acceptance"
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    with pytest.raises(ValueError):
        owner_reconciliation(db, identity, "authorize_retry", "Provider checks remain inconclusive", False)
    result = owner_reconciliation(db, identity, "authorize_retry", "Provider checks remain inconclusive; owner accepts duplicate risk", True)
    assert result["state"] == "assessing"
    assert queue_application(db, settings, identity)["queued"] is False
    assert len(provider.sent) == 1


def test_owner_message_association_is_append_only_and_newsletter_never_confirms(db, settings, ready):
    identity, _, _ = ready
    with db.write() as session:
        email = record_message(session, {"external_id": "fixture-news", "sender": "news@employer.example",
            "subject": "Thank you for applying", "body": "Newsletter about applying", "list_id": "newsletter"})
    result = associate_message(db, email["id"], identity, "Owner associates informational newsletter with this employer")
    assert result["state"] != "submitted_confirmed"
    assert result["messages"][0]["id"] == email["id"]
    with pytest.raises(ValueError, match="already associated"):
        associate_message(db, email["id"], identity, "Repeated association must preserve evidence")


def test_owner_digest_requires_both_switches_and_has_one_effect(db, settings, tmp_path):
    provider = MailSimulator(tmp_path / "notifications.json")
    with db.write() as session:
        session.add(Notification(id="digest-fixture", key="digest:fixture", data={"title": "Fixture digest", "body": "No live applications"}))
        connection = session.get(Connection, "gmail")
        connection.status, connection.data = "connected", {"email_address": "owner@example.test"}
    assert dispatch_notification(db, settings, provider=provider)["status"] == "disabled"
    settings.live_submissions_enabled = settings.email_notifications_enabled = True
    assert dispatch_notification(db, settings, provider=provider)["status"] == "sent_unconfirmed"
    assert dispatch_notification(db, settings, provider=provider)["status"] == "idle"
    assert len(provider.sent) == 1


def followup_policy(db, identity):
    with db.write() as session:
        config = dict(latest_policy(session).data)
        session.add(Answer(id="followup-text", key="followup.text", context="global", data={"confirmed": True,
            "revision": "v1", "value": "I am following up on my application. Thank you for your consideration."}))
        session.add(PolicyRevision(id="followup-policy", data={**config, "followup_enabled": True,
            "followup_after_days": 7, "followup_max_count": 1, "followup_answer_id": "followup-text",
            "answer_revisions": {"followup-text": "v1"}}))
        # Independent immutable historical evidence, never alter an existing event.
        session.add(AuditEvent(entity_id=identity, kind="dispatch_result",
            created_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(), data={"outcome": "accepted"}))


def test_followup_bounded_and_cancelled_after_reply(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    assert dispatch_followup(db, settings, provider=provider)["status"] == "idle"
    followup_policy(db, identity)
    assert dispatch_followup(db, settings, provider=provider)["status"] == "accepted"
    assert dispatch_followup(db, settings, provider=provider)["status"] == "idle"
    assert len(provider.sent) == 2
    with db.read() as session:
        assert session.get(Application, identity).state == "sent_unconfirmed"
        assert len(session.scalars(select(OutboxEvent).where(OutboxEvent.kind == "followup")).all()) == 1


def test_any_recruiter_reply_prevents_followup(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    followup_policy(db, identity)
    with db.write() as session:
        session.add(AuditEvent(entity_id=identity, kind="reply_received"))
    assert dispatch_followup(db, settings, provider=provider)["status"] == "idle"
    assert len(provider.sent) == 1
