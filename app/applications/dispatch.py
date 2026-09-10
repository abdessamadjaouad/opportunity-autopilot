"""The only application-effect dispatcher. Persist intent before provider contact."""
import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from sqlalchemy import select

from app.applications.contracts import DefinitiveRejection, EmailAttachment, EmailEnvelope
from app.core.models import Controls, Decision, Disposition, Facts, Gate, History, Listing, Mode, Policy
from app.core.readiness import evaluate_readiness
from app.models import (Answer, Application, Assessment, AuditEvent, Candidate, Connection,
    Control, DocumentArtifact, HumanTask, Opportunity, OutboxEvent, Packet, ProfileFact, SubmissionAttempt, now_iso, uid)
from app.services import (audit, digest, latest_policy, must_get, task)


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def submission_history(session, application, ignore_attempt_id=None):
    events = session.scalars(select(AuditEvent).where(AuditEvent.entity_id == application.id)
                             .order_by(AuditEvent.created_at)).all()
    if application.data.get("has_submission_evidence") or any(e.kind in {
        "manual_submission_reported", "submission_confirmed"} for e in events):
        return History.CONFIRMED
    attempts = session.scalars(select(SubmissionAttempt).where(SubmissionAttempt.application_id == application.id)).all()
    waived = {e.data.get("attempt_id") for e in events if e.kind == "uncertain_retry_approved"}
    outcomes = {e.data.get("attempt_id"): e.data.get("outcome") for e in events if e.kind == "dispatch_result"}
    for attempt in attempts:
        if attempt.id in waived or attempt.id == ignore_attempt_id:
            continue
        outcome = outcomes.get(attempt.id)
        if outcome == "accepted" or any(e.kind == "provider_acceptance_reconciled" and
                                         e.data.get("attempt_id") == attempt.id for e in events):
            return History.SENT
        if outcome != "not_accepted":
            return History.UNCERTAIN
    if application.state in {"sent_unconfirmed", "submitted_confirmed", "submitting", "submission_uncertain"} and not (
            ignore_attempt_id and application.state == "submitting"):
        return {"sent_unconfirmed": History.SENT, "submitted_confirmed": History.CONFIRMED}.get(
            application.state, History.UNCERTAIN)
    return History.NONE


def advertised_email(opportunity, destination):
    if not isinstance(destination, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", destination):
        return False
    if not opportunity.data.get("is_primary") and not opportunity.data.get("verification_evidence"):
        return False
    text = opportunity.data.get("description", "")
    for match in re.finditer(re.escape(destination), text, re.I):
        surrounding = text[max(0, match.start() - 180):match.end() + 180].lower()
        if re.search(r"apply|application|candidature|postuler|envoyer.*cv|send.*cv", surrounding):
            return True
    return False


def _gate_value(value):
    try:
        return Gate(value)
    except (ValueError, TypeError):
        return Gate.UNKNOWN


def packet_artifacts(session, packet):
    rows = session.scalars(select(DocumentArtifact).where(DocumentArtifact.packet_id == packet.id)).all()
    for identity in packet.data.get("supplied_artifact_ids", []):
        rows.append(must_get(session, DocumentArtifact, identity))
    return rows


def validate_packet_files(session, settings, packet):
    manifest = packet.data.get("manifest", {})
    if packet.status != "validated" or digest(manifest) != packet.data.get("manifest_hash"):
        return False
    if packet.data.get("validation", {}).get("valid") is not True:
        return False
    expected = {(x["filename"], x["sha256"]) for x in manifest.get("document_hashes", [])}
    actual = set()
    for artifact in packet_artifacts(session, packet):
        path = Path(artifact.path).resolve()
        if not path.is_relative_to(settings.artifacts_dir.resolve()) or not path.is_file():
            return False
        if path.stat().st_size != artifact.size_bytes or artifact.size_bytes > 10_000_000:
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
            return False
        actual.add((artifact.data.get("filename"), artifact.sha256))
    return bool(actual) and actual == expected


def evaluate_application(session, settings, identity, *, current_attempt=None, browser_registry=None):
    application = must_get(session, Application, identity)
    opportunity = must_get(session, Opportunity, application.opportunity_id)
    candidate = must_get(session, Candidate, "owner")
    current_policy = latest_policy(session)
    config = current_policy.data
    control = must_get(session, Control, "global").data
    packet = session.get(Packet, application.data.get("packet_id")) if application.data.get("packet_id") else None
    if packet is None:
        packet = session.scalar(select(Packet).where(Packet.application_id == identity).order_by(Packet.created_at.desc()).limit(1))
    manifest = packet.data.get("manifest", {}) if packet else {}
    assessment = session.get(Assessment, manifest.get("assessment_id")) if manifest.get("assessment_id") else None
    evaluated = assessment.data if assessment and assessment.profile_revision == candidate.revision and \
        assessment.posting_hash == opportunity.content_hash else {}
    destination = opportunity.data.get("destination", "")
    reasons = []
    if application.state in {"interview", "offer", "rejected", "withdrawn", "skipped"}:
        return Decision(Disposition.SKIP, ("application_already_handled",)), application, opportunity, packet
    if not settings.live_submissions_enabled:
        return Decision(Disposition.HOLD, ("live_submissions_disabled",)), application, opportunity, packet
    if opportunity.data.get("is_fixture"):
        reasons.append("fixture_opportunity")
    if manifest.get("destination") != destination:
        reasons.append("destination_changed")
    if packet and packet.data.get("family") not in config.get("allowed_document_families", []):
        reasons.append("document_family_outside_policy")
    if opportunity.data.get("deadline_text") and not opportunity.data.get("deadline_timezone"):
        reasons.append("deadline_timezone_unknown")
    reasons += evaluated.get("blockers", [])
    reasons += [x.data.get("reason", "unresolved_human_task") for x in session.scalars(select(HumanTask).where(
        HumanTask.application_id == application.id, HumanTask.status == "open"))]
    selected = manifest.get("selected_facts", [])
    facts_confirmed = bool(selected)
    for item in selected:
        fact = session.get(ProfileFact, item.get("id"))
        if (not fact or fact.status != "confirmed" or fact.revision != item.get("revision") or
                digest(fact.data.get("value")) != item.get("value_hash")):
            facts_confirmed = False
    answers_confirmed = True
    for answer_id, approved_revision in config.get("answer_revisions", {}).items():
        answer = session.get(Answer, answer_id)
        if not answer or answer.data.get("revision") != approved_revision:
            answers_confirmed = False
            reasons.append("answer_scope_requires_renewed_approval")
    for answer_id, packet_revision in manifest.get("answer_revisions", {}).items():
        answer = session.get(Answer, answer_id)
        if (not answer or answer.data.get("confirmed") is not True or answer.data.get("revision") != packet_revision
                or config.get("answer_revisions", {}).get(answer_id) != packet_revision):
            answers_confirmed = False
    connection = session.get(Connection, "gmail" if opportunity.route == "email" else "browser")
    if opportunity.route == "email" and (not config.get("sender_email") or not connection or
            config["sender_email"] != connection.data.get("email_address", "").casefold()):
        reasons.append("sender_scope_requires_renewed_approval")
    from app.workers.budget import BudgetExceeded, check_budget
    try:
        check_budget(session, settings, requests=0 if current_attempt else 1)
        budget_available = True
    except BudgetExceeded:
        budget_available = False
    day = datetime.now(ZoneInfo("Africa/Casablanca")).date().isoformat()
    attempts_today = sum(x.data.get("quota_day") == day for x in session.scalars(select(SubmissionAttempt)))
    cap = min(control.get("daily_cap", 0), config.get("daily_cap", 0))
    remaining = cap - attempts_today + int(current_attempt is not None)
    route_permission = advertised_email(opportunity, destination) if opportunity.route == "email" else False
    if opportunity.route == "browser":
        from app.browser import AdapterRegistry
        registry = browser_registry or AdapterRegistry.load(settings.browser_adapter_registry)
        route_permission = registry.configured_for(destination)
    listing = Listing.OPEN if opportunity.availability == "open" else Listing.CLOSED if opportunity.availability in {"closed", "expired"} else Listing.UNKNOWN
    facts = Facts(opportunity_id=opportunity.id, listing=listing,
        verified_at=instant(opportunity.data.get("last_verified_at")), deadline=instant(opportunity.data.get("deadline")),
        history=submission_history(session, application, current_attempt), eligibility=_gate_value(evaluated.get("eligibility")),
        relocation=_gate_value(evaluated.get("relocation")), route_permission=Gate.PASS if route_permission else Gate.UNKNOWN,
        country=opportunity.country, track=opportunity.track, route=opportunity.route,
        destination_domain=(urlsplit(destination).hostname or "") if opportunity.route == "browser" else
            destination.rsplit("@", 1)[-1].lower() if "@" in destination else "",
        strong_fit=evaluated.get("fit") == "strong", profile_confirmed=facts_confirmed,
        profile_revision=candidate.revision, posting_hash=opportunity.content_hash,
        packet_profile_revision=manifest.get("profile_revision", ""), packet_posting_hash=manifest.get("posting_hash", ""),
        packet_policy_revision=manifest.get("policy_revision", ""),
        documents_validated=validate_packet_files(session, settings, packet) if packet else False,
        answers_confirmed=answers_confirmed, blockers=tuple(sorted(set(reasons))))
    policy = Policy(revision=current_policy.id, approved=config.get("approved") is True,
        mode=Mode.AUTOPILOT if config.get("mode") == "autopilot" else Mode.DRAFT_ONLY,
        allowed_countries=frozenset(config.get("allowed_countries", [])),
        allowed_tracks=frozenset(config.get("allowed_tracks", [])), allowed_routes=frozenset(config.get("allowed_routes", [])),
        allowed_domains=frozenset(config.get("allowed_domains", [])), max_verification_age=timedelta(minutes=30))
    controls = Controls(paused=control.get("paused", True), remaining_daily_slots=remaining,
        budget_available=budget_available, connection_healthy=route_permission if opportunity.route == "browser" else
            bool(connection and connection.status in {"healthy", "connected"}))
    return evaluate_readiness(facts, policy, controls, now=datetime.now(UTC)), application, opportunity, packet


def queue_application(db, settings, identity, *, browser_registry=None):
    with db.write() as session:
        decision, application, opportunity, packet = evaluate_application(session, settings, identity, browser_registry=browser_registry)
        if decision.disposition is not Disposition.READY:
            return {"disposition": decision.disposition.value, "reasons": list(decision.reasons), "queued": False}
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.dedupe_key == f"submit:{identity}"))
        if event and event.state not in {"cancelled", "failed_safe"}:
            return {"disposition": "hold", "reasons": ["already_queued_or_dispatched"], "queued": False}
        if event:
            event.state, event.data = "pending", {"packet_id": packet.id}
        else:
            event = OutboxEvent(application_id=identity, dedupe_key=f"submit:{identity}", data={"packet_id": packet.id})
            session.add(event)
        application.state, application.updated_at = "ready", now_iso()
        audit(session, identity, "dispatch_queued", packet_id=packet.id)
        session.flush()
        return {"disposition": "ready", "reasons": [], "queued": True, "outbox_id": event.id}


def _envelope(session, opportunity, packet, message_id):
    manifest = packet.data["manifest"]
    requested = {x["kind"] for x in manifest["requested_documents"]}
    attachments = []
    for artifact in packet_artifacts(session, packet):
        kind = artifact.data.get("kind")
        if kind in requested and kind != "motivation_text":
            if artifact.mime_type != "application/pdf":
                raise ValueError("Only reviewed PDF attachments are supported by email dispatch")
            attachments.append(EmailAttachment(artifact.data["filename"], artifact.mime_type, Path(artifact.path).read_bytes()))
    subject = manifest["subject"]
    if any(c in subject for c in "\r\n") or len(subject) > 998:
        raise ValueError("Invalid application subject")
    text = manifest.get("answers", {}).get("motivation_text") or (
        f"Please find my application for {opportunity.title}.\n\nThank you for considering my application.")
    return EmailEnvelope(message_id=message_id, recipient=manifest["destination"], subject=subject,
                         text=text, attachments=tuple(attachments))


def browser_packet(session, opportunity, packet):
    manifest = packet.data["manifest"]
    requested = {x["kind"] for x in manifest.get("requested_documents", [])}
    answers = dict(manifest.get("answers", {}))
    for identity in manifest.get("answer_revisions", {}):
        answer = must_get(session, Answer, identity)
        if answer.data.get("context", "global") in {"global", opportunity.id}:
            answers[answer.key] = answer.data.get("value")
    return {"url": opportunity.data.get("destination", ""), "application_id": packet.application_id,
        "manifest_hash": packet.data["manifest_hash"], "answers": answers,
        "documents": [{"kind": a.data.get("kind"), "path": a.path, "sha256": a.sha256}
                      for a in packet_artifacts(session, packet) if a.data.get("kind") in requested and
                      a.mime_type == "application/pdf"]}


def final_effect_gate(db, settings, application_id, attempt_id, *, browser_registry=None):
    with db.write() as session:
        attempt = must_get(session, SubmissionAttempt, attempt_id)
        outbox = session.scalar(select(OutboxEvent).where(OutboxEvent.application_id == application_id,
                                                       OutboxEvent.state == "dispatching"))
        if not outbox or outbox.data.get("attempt_id") != attempt_id:
            return False
        decision, _, _, packet = evaluate_application(session, settings, application_id,
            current_attempt=attempt_id, browser_registry=browser_registry)
        return decision.disposition is Disposition.READY and packet.data["manifest_hash"] == attempt.data["manifest_hash"]


def dispatch_next(db, settings, *, provider=None, before_final_gate=None, after_acceptance=None):
    if not settings.live_submissions_enabled:
        return {"status": "held", "reason": "live_submissions_disabled"}
    with db.read() as session:
        candidate = next((x for x in session.scalars(select(OutboxEvent).where(OutboxEvent.state == "pending",
            OutboxEvent.kind == "submit").order_by(OutboxEvent.created_at)) if x.data.get("next_attempt_at", "") <= now_iso()), None)
        if not candidate:
            return {"status": "idle"}
        outbox_id, application_id = candidate.id, candidate.application_id
        application = must_get(session, Application, application_id)
        opportunity = must_get(session, Opportunity, application.opportunity_id)
        last_verified = instant(opportunity.data.get("last_verified_at"))
        refresh_needed = not last_verified or datetime.now(UTC) - last_verified > timedelta(minutes=30)
    if refresh_needed:
        from app.opportunities.discovery import verify_opportunity
        verify_opportunity(db, settings, opportunity.id)
    if before_final_gate:
        before_final_gate()
    with db.write() as session:
        outbox = must_get(session, OutboxEvent, outbox_id)
        if outbox.state != "pending":
            return {"status": "held", "reason": "outbox_no_longer_pending"}
        registry = getattr(provider, "registry", None)
        decision, application, opportunity, packet = evaluate_application(session, settings, application_id, browser_registry=registry)
        if decision.disposition is not Disposition.READY:
            outbox.data = {**outbox.data, "last_gate_reasons": list(decision.reasons),
                "next_attempt_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
            if decision.disposition is Disposition.SKIP:
                outbox.state = "cancelled"
            return {"status": "held", "reasons": list(decision.reasons)}
        message_id = f"<oa-{uid()}@opportunity-autopilot.local>"
        envelope = _envelope(session, opportunity, packet, message_id) if opportunity.route == "email" else None
        browser_input = browser_packet(session, opportunity, packet) if opportunity.route == "browser" else None
        attempt = SubmissionAttempt(id=uid(), application_id=application_id, message_id=message_id,
            data={"packet_id": packet.id, "policy_id": latest_policy(session).id,
                  "manifest_hash": packet.data["manifest_hash"], "destination": opportunity.data["destination"],
                  "quota_day": datetime.now(ZoneInfo("Africa/Casablanca")).date().isoformat(),
                  "dispatch_started_at": now_iso(), "provider": "simulator" if provider and provider.is_simulator else opportunity.route})
        session.add(attempt)
        from app.workers.budget import reserve
        reserve(session, settings, f"dispatch:{attempt.id}", "submission")
        outbox.state, outbox.data = "dispatching", {**outbox.data, "attempt_id": attempt.id, "started_at": now_iso()}
        application.state, application.updated_at = "submitting", now_iso()
        audit(session, application_id, "dispatch_started", attempt_id=attempt.id, outbox_id=outbox.id,
              note="External request may be in flight; pause cannot recall it")
        attempt_id = attempt.id
    # After this commit a crash is uncertain, even if it occurred just before the request.
    try:
        if provider is None:
            if browser_input:
                from app.browser import BrowserProvider
                provider = BrowserProvider(db, settings)
            else:
                from app.integrations.gmail import get_gmail_provider
                provider = get_gmail_provider(db, settings)
        def final_gate():
            return final_effect_gate(db, settings, application_id, attempt_id,
                                    browser_registry=getattr(provider, "registry", None))
        if browser_input:
            accepted = provider.submit(browser_input, attempt_id=attempt_id, before_external_write=final_gate)
        else:
            provider.before_external_write = final_gate
            if not final_gate():
                raise DefinitiveRejection("final_effect_gate_refused")
            accepted = provider.send(envelope)
        if not accepted.provider_id:
            raise RuntimeError("Provider returned no acceptance evidence")
        if after_acceptance:
            after_acceptance(accepted)
        confirmed = bool(browser_input and accepted.evidence and accepted.evidence.get("kind") == "browser_receipt")
        outcome, state, evidence = "accepted", "submitted_confirmed" if confirmed else "sent_unconfirmed", {
            "provider_id": accepted.provider_id, "thread_id": accepted.thread_id, "message_id": message_id,
            "provider_evidence": accepted.evidence or {}, "has_submission_evidence": confirmed}
    except DefinitiveRejection as exc:
        outcome, state, evidence = "not_accepted", "needs_human", {"code": exc.code,
            "handoff": getattr(exc, "evidence", {}), "reasons": getattr(exc, "reasons", [exc.code])}
    except Exception as exc:
        outcome, state, evidence = "uncertain", "submission_uncertain", {"error": type(exc).__name__, "message_id": message_id}
    with db.write() as session:
        application, outbox = must_get(session, Application, application_id), must_get(session, OutboxEvent, outbox_id)
        application.state, application.updated_at = state, now_iso()
        application.data = {**application.data, **evidence, "simulation": provider.is_simulator if provider else False}
        outbox.state = "failed_safe" if outcome == "not_accepted" else "uncertain" if outcome == "uncertain" else "done"
        audit(session, application_id, "dispatch_result", attempt_id=attempt_id, outcome=outcome, **evidence)
        if outcome != "accepted":
            task(session, f"submission:{application_id}", "Submission needs reconciliation" if outcome == "uncertain" else "Provider rejected request",
                 "submission_uncertain" if outcome == "uncertain" else "provider_not_accepted", application=application,
                 context={"url": opportunity.url, "packet_id": packet.id, "attempt_id": attempt_id,
                          "evidence": evidence}, priority=0)
            from app.workers.notifications import notify
            notify(session, f"submission:{application_id}", "Submission needs attention", state,
                   urgency="urgent", context={"application_id": application_id})
    return {"status": state, "attempt_id": attempt_id, "simulation": provider.is_simulator if provider else False}


def recover_inflight(db, *, older_than_seconds=300):
    cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
    recovered = []
    with db.write() as session:
        for outbox in session.scalars(select(OutboxEvent).where(OutboxEvent.state == "dispatching")):
            if outbox.data.get("started_at", "") > cutoff:
                continue
            if outbox.kind != "submit":
                outbox.state = "uncertain"
                audit(session, outbox.application_id or "owner", "auxiliary_dispatch_uncertain", outbox_id=outbox.id)
                if outbox.kind == "notification":
                    from app.models import Notification
                    notification = session.get(Notification, outbox.data.get("notification_id"))
                    if notification:
                        notification.data = {**notification.data, "external_delivery": "uncertain"}
                elif outbox.application_id:
                    application = must_get(session, Application, outbox.application_id)
                    task(session, f"followup:{application.id}", "Follow-up delivery uncertain", "followup_uncertain",
                         application=application, context={"attempt_id": outbox.data.get("attempt_id")}, priority=0)
                continue
            application = must_get(session, Application, outbox.application_id)
            outbox.state, application.state = "uncertain", "submission_uncertain"
            audit(session, application.id, "dispatch_recovered_uncertain", attempt_id=outbox.data.get("attempt_id"))
            task(session, f"submission:{application.id}", "Reconcile interrupted submission", "submission_uncertain",
                 application=application, context={"attempt_id": outbox.data.get("attempt_id")}, priority=0)
            recovered.append(application.id)
    return recovered
