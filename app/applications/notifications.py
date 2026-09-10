from sqlalchemy import select

from app.applications.contracts import DefinitiveRejection, EmailEnvelope
from app.models import Connection, Notification, OutboxEvent, now_iso, uid
from app.services import audit, must_get
from app.workers.budget import reserve


def dispatch_notification(db, settings, *, provider=None):
    if not settings.email_notifications_enabled or not settings.live_submissions_enabled:
        return {"status": "disabled"}
    with db.write() as session:
        connection = must_get(session, Connection, "gmail")
        recipient = connection.data.get("email_address")
        if connection.status != "connected" or not recipient:
            return {"status": "unconfigured"}
        notification = next((x for x in session.scalars(select(Notification).order_by(Notification.created_at))
            if (x.key.startswith("digest:") or x.data.get("urgency") == "urgent") and not session.scalar(
                select(OutboxEvent).where(OutboxEvent.dedupe_key == f"notification:{x.id}"))), None)
        if not notification:
            return {"status": "idle"}
        identity, message_id = uid(), f"<oa-notify-{uid()}@opportunity-autopilot.local>"
        reserve(session, settings, f"notification:{identity}", "owner_notification")
        event = OutboxEvent(id=identity, kind="notification", state="dispatching",
            dedupe_key=f"notification:{notification.id}", data={"notification_id": notification.id,
                "message_id": message_id, "recipient": recipient, "started_at": now_iso()})
        session.add(event)
        envelope = EmailEnvelope(message_id, recipient, "Opportunity Autopilot: " + notification.data["title"],
                                 notification.data["body"], ())
        audit(session, "owner", "notification_dispatch_started", outbox_id=identity, message_id=message_id)
    def final_gate():
        with db.read() as session:
            connection = must_get(session, Connection, "gmail")
            return bool(settings.email_notifications_enabled and settings.live_submissions_enabled and
                        connection.status == "connected" and connection.data.get("email_address") == recipient)
    try:
        if provider is None:
            from app.integrations.gmail import get_gmail_provider
            provider = get_gmail_provider(db, settings)
        provider.before_external_write = final_gate
        if not final_gate():
            raise DefinitiveRejection("notification_scope_changed")
        accepted = provider.send(envelope)
        status, evidence = "sent_unconfirmed", {"provider_id": accepted.provider_id}
    except DefinitiveRejection as exc:
        status, evidence = "not_accepted", {"code": exc.code}
    except Exception as exc:
        status, evidence = "uncertain", {"error": type(exc).__name__}
    with db.write() as session:
        event = must_get(session, OutboxEvent, identity)
        event.state = "done" if status == "sent_unconfirmed" else status
        event.data = {**event.data, **evidence}
        notification = must_get(session, Notification, notification.id)
        notification.data = {**notification.data, "external_delivery": status, "simulation": provider.is_simulator if provider else False}
        audit(session, "owner", "notification_dispatch_result", outbox_id=identity, status=status, **evidence)
    return {"status": status}
