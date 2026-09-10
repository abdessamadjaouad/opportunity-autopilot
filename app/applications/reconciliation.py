import re
from email.utils import parseaddr

from sqlalchemy import select

from app.models import (Application, Connection, EmailMessage, HumanTask, OutboxEvent,
                        SubmissionAttempt, AuditEvent, now_iso)
from app.services import application_view, audit, must_get, record_json, task
from app.workers.notifications import notify


def reconcile_application(db, settings, identity, *, provider=None):
    if provider is None:
        from app.integrations.gmail import get_gmail_provider
        provider = get_gmail_provider(db, settings)
    with db.read() as session:
        application = must_get(session, Application, identity)
        attempts = session.scalars(select(SubmissionAttempt).where(SubmissionAttempt.application_id == identity)
            .order_by(SubmissionAttempt.created_at.desc())).all()
        attempts = [x for x in attempts if x.data.get("kind") != "followup"]
    found = []
    for attempt in attempts:
        for evidence in provider.find_sent(attempt.message_id):
            if evidence.get("message_id") != attempt.message_id or not evidence.get("provider_id"):
                continue
            recipient = evidence.get("recipient") or evidence.get("to")
            if recipient and parseaddr(recipient)[1].casefold() != attempt.data["destination"].casefold():
                continue
            found.append((attempt, evidence))
    with db.write() as session:
        application = must_get(session, Application, identity)
        if found:
            attempt, evidence = found[0]
            if application.state in {"submitting", "submission_uncertain", "needs_human", "ready"}:
                application.state = "sent_unconfirmed"
            application.updated_at = now_iso()
            application.data = {**application.data, "provider_id": evidence["provider_id"],
                "thread_id": evidence.get("thread_id", ""), "message_id": attempt.message_id}
            audit(session, identity, "provider_acceptance_reconciled", attempt_id=attempt.id,
                provider_id=evidence["provider_id"], note="Sent Mail is acceptance evidence, not employer confirmation")
            for outbox in session.scalars(select(OutboxEvent).where(OutboxEvent.application_id == identity,
                                                                  OutboxEvent.state.in_(["uncertain", "dispatching"]))):
                outbox.state = "done"
            for item in session.scalars(select(HumanTask).where(HumanTask.application_id == identity,
                                                                HumanTask.group_key == f"submission:{identity}")):
                item.status = "resolved"
                item.data = {**item.data, "resolution": "Provider acceptance reconciled; awaiting employer confirmation"}
            if len({x[1]["provider_id"] for x in found}) > 1:
                notify(session, f"duplicate-send:{identity}", "Multiple submission effects detected",
                    "Review provider evidence before further action", urgency="urgent", context={"application_id": identity})
                audit(session, identity, "duplicate_send_detected", provider_ids=[x[1]["provider_id"] for x in found])
        elif attempts and application.state in {"submitting", "submission_uncertain"}:
            application.state = "submission_uncertain"
            task(session, f"submission:{identity}", "Submission remains uncertain", "submission_uncertain",
                 application=application, question="Sent Mail lookup found no proof. Absence is not proof of non-acceptance.",
                 context={"message_ids": [x.message_id for x in attempts]}, priority=0)
        return application_view(session, application, True)


def _association(session, message):
    if message.get("provider") == "gmail" and message.get("authenticated_sender") is not True:
        return None
    refs = " ".join([str(message.get("in_reply_to", "")), str(message.get("references", ""))])
    sender = parseaddr(message.get("sender", ""))[1].lower()
    sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    candidates = []
    for application in session.scalars(select(Application)):
        attempts = session.scalars(select(SubmissionAttempt).where(SubmissionAttempt.application_id == application.id)).all()
        linked = any(x.message_id in refs for x in attempts) or (message.get("thread_id") and
            message["thread_id"] == application.data.get("thread_id"))
        if not linked:
            continue
        destination_domains = {x.data["destination"].rsplit("@", 1)[-1].lower() for x in attempts}
        bounce = bool(re.search(r"mailer-daemon|postmaster", sender)) and any(x.message_id in refs for x in attempts)
        if sender_domain in destination_domains or bounce:
            candidates.append(application)
    return candidates[0] if len(candidates) == 1 else None


def record_message(session, message):
    external_id = message.get("external_id") or message.get("provider_id")
    if not external_id:
        raise ValueError("Mailbox message has no stable provider identifier")
    previous = session.scalar(select(EmailMessage).where(EmailMessage.external_id == external_id))
    if previous:
        return record_json(previous)
    if message.get("direction") == "sent" or "SENT" in message.get("label_ids", message.get("labels", [])):
        return {"status": "sent_message_ignored"}
    application = None if message.get("list_id") or message.get("precedence") in {"bulk", "list"} else _association(session, message)
    email = EmailMessage(external_id=external_id, application_id=application.id if application else None,
                         data=message)
    session.add(email)
    session.flush()
    if not application:
        task(session, f"reply:{external_id}", "Associate an incoming message", "ambiguous_reply",
             question="Match this message to the correct application before changing its status.",
             context={"email_id": email.id, "subject": message.get("subject", ""), "sender": message.get("sender", "")})
        return record_json(email)
    return apply_message(session, email, application)


def apply_message(session, email, application):
    message, external_id = email.data, email.external_id
    body = "" if message.get("list_id") or message.get("precedence") in {"bulk", "list"} else (
        message.get("subject", "") + " " + message.get("body", "")).casefold()
    kind = "reply_received"
    if re.search(r"delivery.*fail|undeliver|address not found|message.*rejected|mail delivery subsystem", body):
        kind = "email_bounced"
        task(session, f"bounce:{application.id}", "Application email bounced", "email_bounced", application=application,
             context={"email_id": email.id}, priority=0)
    elif re.search(r"interview invitation|invite.*interview|schedule.*interview|invitation.*entretien|propos.*entretien", body):
        kind, application.state = "interview_invitation", "interview"
    elif re.search(r"will not be proceeding|decided not to proceed|application.*unsuccessful|regret to inform|candidature.*(?:n.a pas|non|pas).*retenue|application.*rejected", body):
        kind, application.state = "rejection_received", "rejected"
    elif re.search(r"we have received your application|application (?:has been )?received|thank you for applying|candidature.*bien re[cç]ue", body) and not re.search(r"not received|have not|never received", body):
        kind = "submission_confirmed"
        if application.state not in {"interview", "offer", "rejected", "withdrawn"}:
            application.state = "submitted_confirmed"
        application.data = {**application.data, "has_submission_evidence": True,
                            "confirmation_kind": "correlated_acknowledgment", "confirmation_email_id": email.id}
    application.updated_at = now_iso()
    audit(session, application.id, kind, email_id=email.id, external_id=external_id)
    # Any relevant reply cancels queued follow-up effects.
    for outbox in session.scalars(select(OutboxEvent).where(OutboxEvent.application_id == application.id,
        OutboxEvent.kind == "followup", OutboxEvent.state == "pending")):
        outbox.state = "cancelled"
    notify(session, f"reply:{external_id}", kind.replace("_", " ").capitalize(), message.get("subject", "Incoming application reply"),
           urgency="urgent" if kind in {"interview_invitation", "email_bounced"} else "normal",
           context={"application_id": application.id, "email_id": email.id})
    return record_json(email)


def associate_message(db, email_id, application_id, evidence):
    with db.write() as session:
        email = must_get(session, EmailMessage, email_id)
        application = must_get(session, Application, application_id)
        previous = session.scalar(select(AuditEvent).where(AuditEvent.entity_id == email_id,
                                                           AuditEvent.kind == "email_associated"))
        if email.application_id or previous:
            raise ValueError("Message is already associated; retain the original evidence")
        audit(session, email_id, "email_associated", application_id=application_id, evidence=evidence)
        apply_message(session, email, application)
        item = session.scalar(select(HumanTask).where(HumanTask.group_key == f"reply:{email.external_id}"))
        if item:
            item.status = "resolved"
            item.data = {**item.data, "resolution": evidence, "resolved_at": now_iso()}
        session.flush()
        return application_view(session, application, True)


def owner_reconciliation(db, identity, decision, evidence, accept_duplicate_risk=False):
    from app.applications.dispatch import submission_history
    from app.core.models import History
    from app.services import enqueue_job, invalidate
    with db.write() as session:
        application = must_get(session, Application, identity)
        if application.state not in {"submission_uncertain", "sent_unconfirmed"}:
            raise ValueError("Only uncertain or unconfirmed submissions need this decision")
        attempts = session.scalars(select(SubmissionAttempt).where(SubmissionAttempt.application_id == identity)).all()
        if not attempts:
            raise ValueError("No persisted submission attempt to reconcile")
        if decision == "confirm_receipt":
            application.state = "submitted_confirmed"
            application.data = {**application.data, "has_submission_evidence": True, "confirmation_kind": "owner_reported_receipt"}
            audit(session, identity, "submission_confirmed", evidence=evidence, confirmation_kind="owner_reported_receipt")
        elif decision == "authorize_retry":
            if not accept_duplicate_risk or submission_history(session, application) in {History.SENT, History.CONFIRMED}:
                raise ValueError("Known provider acceptance cannot be retried; uncertainty requires explicit duplicate-risk acceptance")
            for attempt in attempts:
                audit(session, identity, "uncertain_retry_approved", attempt_id=attempt.id, evidence=evidence,
                      accept_duplicate_risk=True)
            application.state = "assessing"
            invalidate(session, reason="owner_authorized_uncertain_retry", opportunity_id=application.opportunity_id)
            enqueue_job(session, f"assess:{application.opportunity_id}", "assess", {"opportunity_id": application.opportunity_id})
        else:
            raise ValueError("Unsupported reconciliation decision")
        application.updated_at = now_iso()
        for outbox in session.scalars(select(OutboxEvent).where(OutboxEvent.application_id == identity,
            OutboxEvent.state.in_(["pending", "uncertain"]))):
            outbox.state = "done" if decision == "confirm_receipt" else "cancelled"
        for item in session.scalars(select(HumanTask).where(HumanTask.group_key == f"submission:{identity}")):
            item.status = "resolved"
            item.data = {**item.data, "resolution": evidence, "resolved_at": now_iso()}
        return application_view(session, application, True)


def poll_mailbox(db, settings, *, provider=None):
    if provider is None:
        from app.integrations.gmail import get_gmail_provider
        provider = get_gmail_provider(db, settings)
    with db.read() as session:
        connection = must_get(session, Connection, "gmail")
        cursor = connection.data.get("cursor")
    result = provider.poll_messages(cursor)
    with db.write() as session:
        records = [record_message(session, x) for x in result.get("messages", [])]
        connection = must_get(session, Connection, "gmail")
        connection.data = {**connection.data, "cursor": result.get("cursor"), "last_poll_at": now_iso()}
    return {"processed": len(records), "cursor": result.get("cursor")}
