from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.connectors.http import ConnectorError, safe_fetch
from app.models import BudgetLedger, Source, uid
from app.workers.budget import BudgetExceeded, reserve


def budgeted_fetcher(db, settings, source_id=None, fetcher=None):
    def fetch(url, **kwargs):
        try:
            with db.write() as session:
                if source_id:
                    source = session.get(Source, source_id)
                    limit = source.data.get("daily_request_cap", 500)
                    today = datetime.now(ZoneInfo("Africa/Casablanca")).date()
                    used = sum(x.data.get("requests", 0) for x in session.scalars(select(BudgetLedger).where(
                        BudgetLedger.kind == f"source:{source_id}")) if datetime.fromisoformat(x.created_at)
                        .astimezone(ZoneInfo("Africa/Casablanca")).date() == today)
                    if used >= limit:
                        raise BudgetExceeded("source_daily_request_limit")
                reserve(session, settings, uid(), f"source:{source_id}" if source_id else "verification")
        except BudgetExceeded as exc:
            raise ConnectorError(str(exc)) from None
        return (fetcher or safe_fetch)(url, **kwargs)
    return fetch
