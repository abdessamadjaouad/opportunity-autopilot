import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from app.applications.dispatch import dispatch_next, queue_application, recover_inflight
from app.applications.reconciliation import poll_mailbox, reconcile_application
from app.integrations.simulator import MailSimulator
from app.models import (Application, Assessment, Candidate, Connection, Control, DocumentArtifact, HumanTask,
    Opportunity, OutboxEvent, Packet, PolicyRevision, ProfileFact, SubmissionAttempt)
from app.services import add_opportunity, digest


@pytest.fixture
def ready(db, settings, tmp_path):
    # This flag and policy exist only in an isolated test database. Provider has no network methods.
    settings.live_submissions_enabled = True
    person = add_opportunity(db, {"url": "https://employer.example/jobs/123", "title": "Fixture data engineer",
        "employer": "Fixture employer", "country": "FR", "route": "email"})
    identity, application_id = person["id"], person["application_id"]
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    path = settings.artifacts_dir / "fixture-cv.pdf"
    path.write_bytes(buffer.getvalue())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with db.write() as session:
        opportunity = session.get(Opportunity, identity)
        opportunity.availability = "open"
        opportunity.data = {**opportunity.data, "is_primary": True,
            "description": "To apply, send your CV to jobs@employer.example. Visa sponsorship available.",
            "destination": "jobs@employer.example", "last_verified_at": __import__("app.models", fromlist=["now_iso"]).now_iso()}
        candidate = session.get(Candidate, "owner")
        candidate.revision = "profile-fixture"
        session.add(ProfileFact(id="fixture-fact", key="identity.name", status="confirmed", revision=1,
            data={"value": "Fixture Owner"}))
        session.add(PolicyRevision(id="policy-fixture", data={"approved": True, "mode": "autopilot", "daily_cap": 2,
            "allowed_countries": ["FR"], "allowed_tracks": ["data_ai"], "allowed_routes": ["email"],
            "allowed_domains": ["employer.example"], "allowed_document_families": ["data_engineering"],
            "sender_email": "owner@example.test"}))
        controls = session.get(Control, "global")
        controls.data = {**controls.data, "daily_cap": 2}
        session.get(Connection, "gmail").status = "connected"
        session.get(Connection, "gmail").data = {"email_address": "owner@example.test"}
        session.add(Assessment(id="assessment-fixture", opportunity_id=identity, profile_revision=candidate.revision,
            posting_hash=opportunity.content_hash, data={"fit": "strong", "eligibility": "pass", "relocation": "pass", "blockers": []}))
        manifest = {"application_id": application_id, "assessment_id": "assessment-fixture",
            "posting_hash": opportunity.content_hash, "profile_revision": candidate.revision, "policy_revision": "policy-fixture",
            "selected_facts": [{"id": "fixture-fact", "revision": 1, "value_hash": digest("Fixture Owner")}],
            "requested_documents": [{"kind": "cv", "required": True}], "answer_revisions": {}, "answers": {},
            "document_hashes": [{"filename": path.name, "sha256": sha, "kind": "cv"}],
            "destination": "jobs@employer.example", "subject": "Application for fixture job", "family": "data_engineering"}
        session.add(Packet(id="packet-fixture", application_id=application_id, status="validated",
            data={"manifest": manifest, "manifest_hash": digest(manifest), "family": "data_engineering", "validation": {"valid": True}}))
        session.flush()
        session.add(DocumentArtifact(packet_id="packet-fixture", path=str(path), sha256=sha,
            size_bytes=path.stat().st_size, mime_type="application/pdf", data={"filename": path.name, "kind": "cv"}))
        session.get(Application, application_id).data = {"packet_id": "packet-fixture"}
        for item in session.scalars(select(HumanTask)):
            item.status = "resolved"
    return application_id, MailSimulator(tmp_path / "simulator.json"), path


def test_concurrent_dispatch_has_one_provider_effect(db, settings, ready):
    identity, provider, _ = ready
    result = queue_application(db, settings, identity)
    assert result["queued"] is True, result
    with ThreadPoolExecutor(max_workers=5) as workers:
        outcomes = list(workers.map(lambda _: dispatch_next(db, settings, provider=provider), range(5)))
    assert len(provider.sent) == 1
    assert any(x["status"] == "sent_unconfirmed" for x in outcomes)
    with db.read() as session:
        assert len(session.scalars(select(SubmissionAttempt)).all()) == 1
        assert session.get(Application, identity).state == "sent_unconfirmed"
    assert queue_application(db, settings, identity)["queued"] is False


def test_timeout_after_acceptance_reconciles_without_retry(db, settings, ready):
    identity, provider, _ = ready
    provider.outcome = "timeout_after_acceptance"
    assert queue_application(db, settings, identity)["queued"]
    assert dispatch_next(db, settings, provider=provider)["status"] == "submission_uncertain"
    assert queue_application(db, settings, identity)["queued"] is False
    result = reconcile_application(db, settings, identity, provider=provider)
    assert result["state"] == "sent_unconfirmed"
    assert len(provider.sent) == 1
    assert dispatch_next(db, settings, provider=provider)["status"] == "idle"


def test_process_crash_after_acceptance_preserves_attempt_and_recovers(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    def crash(_):
        raise SystemExit("Simulated process termination")
    with pytest.raises(SystemExit):
        dispatch_next(db, settings, provider=provider, after_acceptance=crash)
    assert len(provider.sent) == 1
    assert recover_inflight(db, older_than_seconds=0) == [identity]
    assert queue_application(db, settings, identity)["queued"] is False
    assert reconcile_application(db, settings, identity, provider=provider)["state"] == "sent_unconfirmed"
    assert len(provider.sent) == 1


def test_pause_during_queue_prevents_unstarted_dispatch(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    def pause():
        with db.write() as session:
            control = session.get(Control, "global")
            control.data = {**control.data, "paused": True}
    assert dispatch_next(db, settings, provider=provider, before_final_gate=pause)["status"] == "held"
    assert not provider.sent
    with db.read() as session:
        assert not session.scalars(select(SubmissionAttempt)).all()


@pytest.mark.parametrize("change", ["file", "manifest", "policy", "profile", "destination", "budget", "quota", "connection"])
def test_last_gate_rejects_changed_authority_or_packet(db, settings, ready, change):
    identity, provider, path = ready
    assert queue_application(db, settings, identity)["queued"]
    if change == "file":
        path.write_bytes(b"forged")
    else:
        with db.write() as session:
            if change == "manifest":
                packet = session.get(Packet, "packet-fixture")
                packet.data = {**packet.data, "manifest_hash": "forged"}
            elif change == "policy":
                session.add(PolicyRevision(data={"approved": False, "mode": "draft_only"}))
            elif change == "profile":
                session.get(Candidate, "owner").revision = "profile-new"
            elif change == "destination":
                opportunity = session.scalar(select(Opportunity))
                opportunity.data = {**opportunity.data, "destination": "attacker@other.example"}
            elif change in {"budget", "quota"}:
                controls = session.get(Control, "global")
                controls.data = {**controls.data, "daily_request_cap" if change == "budget" else "daily_cap": 0}
            elif change == "connection":
                session.get(Connection, "gmail").status = "revoked"
    assert dispatch_next(db, settings, provider=provider)["status"] == "held"
    assert not provider.sent


def test_correlated_receipt_confirms_but_newsletter_does_not(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    sent = provider.sent[0]
    provider.add_incoming({"external_id": "newsletter", "sender": "news@employer.example",
        "subject": "Weekly jobs", "body": "Thank you for applying"})
    poll_mailbox(db, settings, provider=provider)
    with db.read() as session:
        assert session.get(Application, identity).state == "sent_unconfirmed"
    provider.add_incoming({"external_id": "receipt", "sender": "jobs@employer.example",
        "thread_id": sent["thread_id"], "in_reply_to": sent["message_id"],
        "subject": "Application received", "body": "We have received your application."})
    poll_mailbox(db, settings, provider=provider)
    with db.read() as session:
        assert session.get(Application, identity).state == "submitted_confirmed"
        assert session.get(Application, identity).data["confirmation_kind"] == "correlated_acknowledgment"


def test_spoofed_reply_does_not_match_even_with_thread_id(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    provider.add_incoming({"external_id": "spoof", "sender": "hacker@other.example",
        "thread_id": provider.sent[0]["thread_id"], "subject": "Application received", "body": "Thank you for applying"})
    poll_mailbox(db, settings, provider=provider)
    with db.read() as session:
        assert session.get(Application, identity).state == "sent_unconfirmed"
        assert any(x.data["reason"] == "ambiguous_reply" for x in session.scalars(select(HumanTask)))


def test_interview_and_bounce_are_visible_and_cancel_followups(db, settings, ready):
    identity, provider, _ = ready
    queue_application(db, settings, identity)
    dispatch_next(db, settings, provider=provider)
    with db.write() as session:
        session.add(OutboxEvent(application_id=identity, kind="followup", dedupe_key="fixture-followup"))
    sent = provider.sent[0]
    provider.add_incoming({"external_id": "interview", "sender": "jobs@employer.example", "in_reply_to": sent["message_id"],
        "subject": "Interview invitation", "body": "We would like to schedule an interview."})
    poll_mailbox(db, settings, provider=provider)
    with db.read() as session:
        assert session.get(Application, identity).state == "interview"
        assert session.scalar(select(OutboxEvent).where(OutboxEvent.kind == "followup")).state == "cancelled"
    provider.add_incoming({"external_id": "bounce", "sender": "mailer-daemon@mailer.example", "in_reply_to": sent["message_id"],
        "subject": "Mail delivery subsystem", "body": "Delivery failed"})
    poll_mailbox(db, settings, provider=provider)
    with db.read() as session:
        assert any(x.data["reason"] == "email_bounced" for x in session.scalars(select(HumanTask)))


def test_server_default_never_sends_even_with_fixture_approved_policy(db, settings, ready):
    identity, provider, _ = ready
    settings.live_submissions_enabled = False
    assert queue_application(db, settings, identity)["reasons"] == ["live_submissions_disabled"]
    assert dispatch_next(db, settings, provider=provider)["status"] == "held"
    assert not provider.sent
