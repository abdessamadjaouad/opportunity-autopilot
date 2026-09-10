from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.applications.contracts import DefinitiveRejection, EmailEnvelope
from app.applications.dispatch import advertised_email, instant
from app.models import (Answer, Application, AuditEvent, Connection, Control, Opportunity, OutboxEvent,
                        SubmissionAttempt, now_iso, uid)
from app.services import audit, latest_policy, must_get, task
from app.workers.budget import BudgetExceeded, check_budget, reserve

REPLIES = {"reply_received", "submission_confirmed", "interview_invitation", "rejection_received", "email_bounced"}


def gate(session, settings, application, *, current_attempt=None):
    policy, controls = latest_policy(session).data, must_get(session, Control, "global").data
    if not settings.live_submissions_enabled or controls.get("paused") or policy.get("approved") is not True or \
            policy.get("mode") != "autopilot" or policy.get("followup_enabled") is not True or policy.get("followup_max_count") != 1:
        return None
    if application.state not in {"sent_unconfirmed", "submitted_confirmed"}:
        return None
    if session.scalar(select(AuditEvent).where(AuditEvent.entity_id == application.id, AuditEvent.kind.in_(REPLIES))):
        return None
    if any(x.id != current_attempt for x in session.scalars(select(SubmissionAttempt).where(
            SubmissionAttempt.application_id == application.id)) if x.data.get("kind") == "followup"):
        return None
    previous = session.scalar(select(AuditEvent).where(AuditEvent.entity_id == application.id,
        AuditEvent.kind == "dispatch_result").order_by(AuditEvent.created_at).limit(1))
    if not previous or previous.data.get("outcome") != "accepted" or \
            datetime.now(UTC) - instant(previous.created_at) < timedelta(days=policy.get("followup_after_days", 14)):
        return None
    opportunity = must_get(session, Opportunity, application.opportunity_id)
    verified = instant(opportunity.data.get("last_verified_at"))
    if opportunity.route != "email" or opportunity.availability != "open" or not verified or \
            not timedelta(0) <= datetime.now(UTC) - verified <= timedelta(minutes=30):
        return None
    destination = opportunity.data.get("destination", "")
    initial = session.scalar(select(SubmissionAttempt).where(SubmissionAttempt.application_id == application.id)
                             .order_by(SubmissionAttempt.created_at).limit(1))
    if not initial or initial.data.get("destination") != destination or not advertised_email(opportunity, destination):
        return None
    for value, allowed in [(opportunity.country, "allowed_countries"), (opportunity.track, "allowed_tracks"),
                           ("email", "allowed_routes"), (destination.rsplit("@", 1)[-1].lower(), "allowed_domains")]:
        if value not in policy.get(allowed, []):
            return None
    answer = session.get(Answer, policy.get("followup_answer_id", ""))
    if not answer or answer.data.get("confirmed") is not True or answer.data.get("sensitive") or \
            answer.data.get("context", "global") not in {"global", opportunity.id} or \
            policy.get("answer_revisions", {}).get(answer.id) != answer.data.get("revision") or \
            not isinstance(answer.data.get("value"), str) or not 10 <= len(answer.data["value"]) <= 2000:
        return None
    connection = must_get(session, Connection, "gmail")
    if connection.status != "connected" or connection.data.get("email_address", "").casefold() != policy.get("sender_email"):
        return None
    day = datetime.now(ZoneInfo("Africa/Casablanca")).date().isoformat()
    used = sum(x.data.get("quota_day") == day and x.id != current_attempt for x in session.scalars(select(SubmissionAttempt)))
    if used >= min(controls.get("daily_cap", 0), policy.get("daily_cap", 0)):
        return None
    try:
        check_budget(session, settings, requests=0 if current_attempt else 1)
    except BudgetExceeded:
        return None
    return opportunity, answer.data["value"], destination, day


def dispatch_followup(db, settings, *, provider=None):
    if not settings.live_submissions_enabled:
        return {"status": "disabled"}
    with db.write() as session:
        selected = None
        for application in session.scalars(select(Application)):
            if session.scalar(select(OutboxEvent).where(OutboxEvent.dedupe_key == f"followup:{application.id}")):
                continue
            permitted = gate(session, settings, application)
            if permitted:
                selected = (application, permitted)
                break
        if not selected:
            return {"status": "idle"}
        application, (opportunity, body, destination, day) = selected
        identity, attempt_id = application.id, uid()
        message_id = f"<oa-followup-{uid()}@opportunity-autopilot.local>"
        subject = "Application follow-up: " + opportunity.title
        if len(subject) > 998 or any(c in subject for c in "\r\n"):
            return {"status": "held", "reason": "invalid_subject"}
        session.add(SubmissionAttempt(id=attempt_id, application_id=identity, message_id=message_id,
            data={"kind": "followup", "destination": destination, "quota_day": day,
                  "dispatch_started_at": now_iso(), "policy_id": latest_policy(session).id}))
        event = OutboxEvent(id=uid(), application_id=identity, kind="followup", state="dispatching",
            dedupe_key=f"followup:{identity}", data={"attempt_id": attempt_id, "started_at": now_iso()})
        session.add(event)
        reserve(session, settings, f"followup:{attempt_id}", "followup")
        audit(session, identity, "followup_dispatch_started", attempt_id=attempt_id)
        event_id = event.id
    def final_gate():
        with db.read() as session:
            event = must_get(session, OutboxEvent, event_id)
            return bool(event.state == "dispatching" and gate(session, settings,
                must_get(session, Application, identity), current_attempt=attempt_id))
    try:
        if provider is None:
            from app.integrations.gmail import get_gmail_provider
            provider = get_gmail_provider(db, settings)
        provider.before_external_write = final_gate
        if not final_gate():
            raise DefinitiveRejection("followup_gate_changed")
        accepted = provider.send(EmailEnvelope(message_id, destination, subject, body, ()))
        outcome, evidence = "accepted", {"provider_id": accepted.provider_id, "thread_id": accepted.thread_id}
    except DefinitiveRejection as exc:
        outcome, evidence = "not_accepted", {"code": exc.code}
    except Exception as exc:
        outcome, evidence = "uncertain", {"error": type(exc).__name__}
    with db.write() as session:
        event = must_get(session, OutboxEvent, event_id)
        event.state = "done" if outcome == "accepted" else outcome
        event.data = {**event.data, **evidence}
        audit(session, identity, "followup_dispatch_result", attempt_id=attempt_id, outcome=outcome,
              simulation=provider.is_simulator if provider else False, **evidence)
        if outcome == "uncertain":
            task(session, f"followup:{identity}", "Follow-up delivery uncertain", "followup_uncertain",
                 application=must_get(session, Application, identity), context={"attempt_id": attempt_id}, priority=0)
    return {"status": outcome}
