from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import BudgetLedger, Control
from app.services import must_get


class BudgetExceeded(ValueError):
    pass


def usage(session):
    today = datetime.now(ZoneInfo("Africa/Casablanca")).date().isoformat()
    totals = {"daily_cents": 0, "monthly_cents": 0, "requests": 0, "tokens": 0}
    for item in session.scalars(select(BudgetLedger)):
        day = datetime.fromisoformat(item.created_at).astimezone(ZoneInfo("Africa/Casablanca")).date().isoformat()
        if day[:7] == today[:7]:
            totals["monthly_cents"] += item.amount_cents
        if day == today:
            totals["daily_cents"] += item.amount_cents
            totals["requests"] += item.data.get("requests", 0)
            totals["tokens"] += item.data.get("tokens", 0)
    return totals


def check_budget(session, settings, *, cents=0, requests=1, tokens=0):
    if any(type(x) is not int or x < 0 for x in (cents, requests, tokens)):
        raise ValueError("Budget reservations require nonnegative integer limits")
    controls = must_get(session, Control, "global").data
    if cents and not settings.paid_services_enabled:
        raise BudgetExceeded("paid_services_disabled")
    totals = usage(session)
    for value, limit, increment, reason in (
        (totals["daily_cents"], controls.get("daily_budget_cents", 0), cents, "daily_budget_limit"),
        (totals["monthly_cents"], controls.get("monthly_budget_cents", 0), cents, "monthly_budget_limit"),
        (totals["requests"], controls.get("daily_request_cap", 0), requests, "daily_request_limit"),
        (totals["tokens"], controls.get("daily_token_cap", 0), tokens, "daily_token_limit"),
    ):
        if value + increment > limit:
            raise BudgetExceeded(reason)


def reserve(session, settings, key, kind, *, cents=0, requests=1, tokens=0):
    previous = session.scalar(select(BudgetLedger).where(BudgetLedger.reservation_key == key))
    if previous:
        return previous
    check_budget(session, settings, cents=cents, requests=requests, tokens=tokens)
    record = BudgetLedger(reservation_key=key, kind=kind, amount_cents=cents,
        data={"requests": requests, "tokens": tokens, "reserved_at": datetime.now(UTC).isoformat()})
    session.add(record)
    return record
