from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Notification
from app.services import automation_view, record_json


def notify(session, key, title, body, urgency="normal", context=None):
    existing = session.scalar(select(Notification).where(Notification.key == key))
    if existing:
        existing.status = "unread"
        existing.data = {**existing.data, "title": title, "body": body, "urgency": urgency,
                         "context": context or {}, "updated_at": datetime.now(UTC).isoformat()}
        return existing
    entry = Notification(key=key, data={"title": title, "body": body, "urgency": urgency,
        "context": context or {}, "delivery": "in_app", "external_delivery": "unconfigured"})
    session.add(entry)
    session.flush()
    return entry


def build_digest(db, settings):
    day = datetime.now(UTC).astimezone(ZoneInfo("Africa/Casablanca")).date().isoformat()
    with db.write() as session:
        existing = session.scalar(select(Notification).where(Notification.key == f"digest:{day}"))
        if existing:
            return record_json(existing)
        state = automation_view(session, settings)
        coverage, metrics = state["coverage"], state["metrics"]
        body = (f"{coverage['healthy']} of {coverage['registered']} registered sources checked successfully. "
                f"{coverage['failed']} sources need attention. {metrics['confirmed']} confirmed applications; "
                f"{metrics['uncertain']} uncertain submissions. Daily recorded spend: "
                f"{state['budget']['daily_cents']} cents. Live submissions "
                f"{'enabled' if settings.live_submissions_enabled else 'disabled'}.")
        return record_json(notify(session, f"digest:{day}", f"Daily digest · {day}", body,
            context={"coverage": coverage, "metrics": metrics}))
