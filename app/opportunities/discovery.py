import random
import re
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from email import policy
from email.parser import Parser
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models import (Application, Assessment, Candidate, HumanTask, Opportunity, Source, SourceOccurrence, SourceRun, now_iso, uid)
from app.security.urls import canonical_url
from app.services import (audit, digest, enqueue_job, invalidate, must_get, opportunity_view, record_json, task)

PUBLIC_APIS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
}
RESEARCH_FEED = "https://www.jobs.cam.ac.uk/job/?category=2&format=json"


def infer_email_route(record):
    if not record.data.get("is_primary") or record.data.get("owner_review"):
        return
    description = record.data.get("description", "")
    candidates = []
    for sentence in re.split(r"[\n;]", description):
        addresses = re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sentence)
        if len(addresses) == 1 and re.search(r"\b(send|email|envoyez|envoyer|adressez)\b", sentence, re.I) and \
                re.search(r"\b(cv|resume|curriculum vitae)\b", sentence, re.I):
            candidates.append((addresses[0].rstrip("."), sentence))
    if len(candidates) == 1:
        destination, evidence = candidates[0]
        requested = [{"kind": "cv", "required": True}]
        if re.search(r"cover letter|lettre de motivation", evidence, re.I):
            requested.append({"kind": "cover_letter", "required": True})
        if not re.search(r"transcript|proposal|statement|certificate|relev[eé]|reference", description, re.I):
            record.route = "email"
            record.data = {**record.data, "destination": destination, "requested_documents": requested,
                           "route_evidence": evidence}


def register_source(db, values):
    connector, board = values["connector"], values.get("board_id", "")
    permission = "unreviewed"
    if connector in PUBLIC_APIS:
        if not board:
            raise ValueError("A registered employer board identifier is required")
        url = PUBLIC_APIS[connector].format(board=board)
        permission = "permitted"
    elif connector == "cambridge":
        url, permission = RESEARCH_FEED, "permitted"
    elif connector in {"email_alert", "search"}:
        url = f"{connector}://" + (digest(values.get("query", "")) if connector == "search" else "unconfigured")
        if connector == "search" and values.get("query") and len(values.get("permission_note", "")) >= 20:
            permission = "permitted"
    else:
        url = canonical_url(values["url"])
        host = urlsplit(url).hostname or ""
        if any(host == x or host.endswith("." + x) for x in ("linkedin.com", "indeed.com", "glassdoor.com")):
            raise ValueError("Restricted source: import authorized alerts or an official employer URL")
        if len(values.get("permission_note", "")) >= 20:
            permission = "permitted"
    with db.write() as session:
        previous = session.scalar(select(Source).where(Source.connector == connector, Source.url == url))
        if previous:
            return record_json(previous)
        source = Source(id=uid(), name=values["name"], connector=connector, url=url,
            data={"board_id": board, "query": values.get("query", ""), "enabled": permission == "permitted", "permission_status": permission,
                  "permitted_access_method": "public_get" if permission == "permitted" else "unconfigured",
                  "reviewed_at": now_iso() if permission == "permitted" else None,
                  "permission_note": values.get("permission_note") or "Documented public listing API/feed, read only",
                  "poll_interval_seconds": max(values.get("poll_interval_seconds", 900),
                      86400 if connector == "search" else 1800 if connector in {"html", "cambridge", "rss"} else 300),
                  "next_check_at": now_iso(), "last_success_at": None, "failures": 0,
                  "cursor": None, "occurrence_count": 0, "limits": {"max_pages": 20, "max_bytes": 5_000_000}})
        session.add(source)
        session.flush()
        audit(session, source.id, "source_registered", connector=connector, permission=permission)
        return record_json(source)


def ingest_occurrences(db, source_id, occurrences):
    results = []
    with db.write() as session:
        source = must_get(session, Source, source_id)
        for occurrence in occurrences:
            url = canonical_url(occurrence["url"])
            external_id = str(occurrence.get("external_id") or digest(url))
            key = occurrence.get("authoritative_key") or "url:" + url
            occurrence_key = f"{source_id}:{external_id}"
            existing_occurrence = session.scalar(select(SourceOccurrence).where(
                SourceOccurrence.occurrence_key == occurrence_key))
            opportunity = session.get(Opportunity, existing_occurrence.opportunity_id) if existing_occurrence else None
            if not opportunity:
                opportunity = session.scalar(select(Opportunity).where(Opportunity.canonical_key == key))
            if not opportunity:
                opportunity = session.scalar(select(Opportunity).where(Opportunity.url == url))
            content = {k: v for k, v in occurrence.items() if k not in {
                "verified_at", "first_seen_at", "last_seen_at", "raw_snapshot", "etag", "last_modified"}}
            content_hash = digest(content)
            current = now_iso()
            primary = occurrence.get("is_primary") is True
            availability = occurrence.get("availability", "unverified") if primary else "unverified"
            if availability not in {"open", "closed", "expired", "unverified"}:
                availability = "unverified"
            if not opportunity:
                opportunity = Opportunity(id=uid(), canonical_key=key, title=occurrence["title"][:500],
                    employer=occurrence["employer"][:250], country=occurrence.get("country", ""),
                    track=occurrence.get("track", "data_ai"), route=occurrence.get("route", "manual"),
                    url=url, availability=availability, content_hash=content_hash,
                    data={**content, "first_seen_at": current, "last_seen_at": current,
                        "last_verified_at": occurrence.get("verified_at") if primary else None,
                        "primary_source_id": source_id if primary else None, "is_fixture": False})
                session.add(opportunity)
                session.flush()
                application = Application(id=uid(), opportunity_id=opportunity.id)
                session.add(application)
                session.flush()
                audit(session, application.id, "opportunity_discovered", source_id=source_id, url=url,
                      posting_hash=content_hash)
                # Cautious similarities preserve both roles and ask once, never merge by title alone.
                similar = session.scalars(select(Opportunity).where(Opportunity.employer == opportunity.employer,
                    Opportunity.id != opportunity.id)).all()
                for other in similar:
                    if (other.country == opportunity.country and
                            SequenceMatcher(None, other.title.casefold(), opportunity.title.casefold()).ratio() > .94):
                        task(session, "duplicate:" + ":".join(sorted([other.id, opportunity.id])),
                             "Review possible duplicate roles", "ambiguous_duplicate", application=application,
                             context={"url": url, "other_opportunity_id": other.id, "other_url": other.url})
            else:
                application = session.scalar(select(Application).where(Application.opportunity_id == opportunity.id))
                if primary and opportunity.content_hash != content_hash:
                    audit(session, application.id, "posting_snapshot_changed", previous_hash=opportunity.content_hash,
                          previous_snapshot=opportunity.data, new_hash=content_hash)
                    opportunity.content_hash = content_hash
                    opportunity.title, opportunity.employer = occurrence["title"][:500], occurrence["employer"][:250]
                    opportunity.country = occurrence.get("country", opportunity.country)
                    opportunity.data = {**opportunity.data, **content, "primary_source_id": source_id}
                    # Changes to primary requirements/destination invalidate prior owner review too.
                    opportunity.data.pop("owner_review", None)
                    opportunity.data.pop("destination", None)
                    invalidate(session, reason="posting_changed", opportunity_id=opportunity.id)
                if primary:
                    opportunity.availability = availability
                    opportunity.data = {**opportunity.data, "last_verified_at": occurrence.get("verified_at")}
                opportunity.data = {**opportunity.data, "last_seen_at": current}
            snapshot = {"captured_at": current, "hash": content_hash, "value": occurrence.get("raw_snapshot", content)}
            if existing_occurrence:
                history = existing_occurrence.data.get("snapshots", [])
                if not history or history[-1]["hash"] != content_hash:
                    history = history + [snapshot]
                existing_occurrence.data = {**existing_occurrence.data, "last_seen_at": current,
                    "content_hash": content_hash, "snapshots": history}
            else:
                session.add(SourceOccurrence(opportunity_id=opportunity.id, source_id=source_id,
                    occurrence_key=occurrence_key, data={"url": url, "external_id": external_id,
                    "source_name": source.name, "first_seen_at": current, "last_seen_at": current,
                    "content_hash": content_hash, "snapshots": [snapshot]}))
            if not primary:
                task(session, f"verify:{opportunity.id}", "Verify the official listing", "listing_unverified",
                     application=application, context={"url": url})
                enqueue_job(session, f"verify:{opportunity.id}", "verify", {"opportunity_id": opportunity.id})
            else:
                infer_email_route(opportunity)
            assessment = session.scalar(select(Assessment).where(Assessment.opportunity_id == opportunity.id,
                Assessment.posting_hash == opportunity.content_hash,
                Assessment.profile_revision == must_get(session, Candidate, "owner").revision).limit(1))
            if not assessment:
                enqueue_job(session, f"assess:{opportunity.id}", "assess", {"opportunity_id": opportunity.id})
            results.append(opportunity.id)
        source.data = {**source.data, "occurrence_count": len(session.scalars(select(SourceOccurrence).where(
            SourceOccurrence.source_id == source_id)).all())}
    return list(dict.fromkeys(results))


def poll_source(db, settings, source_id, *, fetcher=None):
    from app.connectors import ConnectorError, fetch_source
    with db.read() as session:
        source = record_json(must_get(session, Source, source_id))
    if not source.get("enabled") or source.get("permission_status") != "permitted":
        raise ValueError("Source polling is disabled or access permission is unreviewed")
    started = now_iso()
    try:
        from app.workers.requests import budgeted_fetcher
        if source["connector"] == "search":
            from app.integrations.optional import search_leads
            result = search_leads(db, settings, source.get("query", ""))
        else:
            result = fetch_source(source, fetcher=budgeted_fetcher(db, settings, source_id, fetcher))
        ids = ingest_occurrences(db, source_id, result["occurrences"]) if not result.get("not_modified") else []
        with db.write() as session:
            stored = must_get(session, Source, source_id)
            interval = source.get("poll_interval_seconds", 900)
            next_delay = 10 if not result.get("complete", True) else int(interval * random.uniform(1, 1.1))
            stored.status = "healthy"
            stored.data = {**stored.data, "last_success_at": now_iso(), "error": None, "failures": 0,
                "next_check_at": (datetime.now(UTC) + timedelta(seconds=next_delay)).isoformat(),
                "cursor": result.get("cursor"), "etag": result.get("etag"),
                "last_modified": result.get("last_modified")}
            session.add(SourceRun(source_id=source_id, data={"started_at": started, "finished_at": now_iso(),
                "status": "unchanged" if result.get("not_modified") else "success", "opportunity_count": len(ids),
                "complete": result.get("complete", True)}))
        # Missing records in an otherwise successful listing are not silently declared closed.
        return {"source_id": source_id, "status": "healthy", "opportunity_ids": ids,
                "not_modified": result.get("not_modified", False)}
    except (ConnectorError, OSError, TimeoutError, ValueError) as exc:
        code = getattr(exc, "code", "integration_unconfigured_or_budget_limited" if isinstance(exc, ValueError) else "network_error")
        with db.write() as session:
            stored = must_get(session, Source, source_id)
            failures = stored.data.get("failures", 0) + 1
            delay = max(getattr(exc, "retry_after_seconds", None) or 0, min(86400, 60 * 2 ** min(failures, 10)))
            stored.status = "rate_limited" if getattr(exc, "status", None) == 429 or code == "rate_limited" else "error"
            stored.data = {**stored.data, "error": code, "failures": failures,
                "next_check_at": (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()}
            session.add(SourceRun(source_id=source_id, data={"started_at": started, "finished_at": now_iso(),
                "status": stored.status, "error": code, "retry_after_seconds": delay}))
            from app.workers.notifications import notify
            notify(session, f"source-error:{source_id}", "Source check failed", f"{stored.name}: {code}",
                   urgency="attention", context={"source_id": source_id})
        return {"source_id": source_id, "status": "error", "error": code}


def verify_opportunity(db, settings, identity, *, fetcher=None):
    from app.connectors import ConnectorError, verify_listing
    with db.read() as session:
        opportunity = record_json(must_get(session, Opportunity, identity))
        source_id = opportunity.get("primary_source_id")
        source = record_json(session.get(Source, source_id)) if source_id else None
        if not source:
            occurrence = session.scalar(select(SourceOccurrence).where(SourceOccurrence.opportunity_id == identity,
                SourceOccurrence.source_id.is_not(None)).limit(1))
            source = record_json(session.get(Source, occurrence.source_id)) if occurrence else None
    try:
        from app.workers.requests import budgeted_fetcher
        result = verify_listing(opportunity, source=source, fetcher=budgeted_fetcher(db, settings, source_id, fetcher))
    except ConnectorError as exc:
        with db.write() as session:
            application = session.scalar(select(Application).where(Application.opportunity_id == identity))
            task(session, f"verify:{identity}", "Official verification needs review", exc.code,
                 application=application, context={"url": opportunity["url"]})
        return {"status": "needs_human", "reason": exc.code}
    with db.write() as session:
        record = must_get(session, Opportunity, identity)
        application = session.scalar(select(Application).where(Application.opportunity_id == identity))
        if result.get("is_primary") is True:
            availability = result.get("availability", "unverified")
            record.availability = availability if availability in {"open", "closed", "expired"} else "unverified"
            snapshot_hash = digest(result.get("raw_snapshot") or result.get("description", ""))
            previous = record.data.get("verification_snapshot_hash") or digest(record.data.get("description", ""))
            if previous != snapshot_hash:
                record.content_hash = digest({"previous": record.content_hash, "verified_snapshot": snapshot_hash})
                invalidate(session, reason="verification_content_changed", opportunity_id=identity)
                record.data = {k: v for k, v in record.data.items() if k not in {"owner_review", "destination"}}
            updates = {k: result[k] for k in ("deadline", "deadline_text", "deadline_timezone", "language",
                "requested_documents", "application_url", "requirements", "route") if k in result}
            record.data = {**record.data, "last_verified_at": result.get("verified_at"),
                "is_primary": True,
                "verification_snapshot_hash": snapshot_hash, "verification_evidence": result.get("evidence", {}),
                "verified_snapshot": result.get("raw_snapshot") or result.get("description", ""),
                "description": result.get("description") or record.data.get("description", ""), **updates}
            infer_email_route(record)
            enqueue_job(session, f"assess:{identity}", "assess", {"opportunity_id": identity})
            item = session.scalar(select(HumanTask).where(HumanTask.group_key == f"verify:{identity}"))
            if item and record.availability == "open":
                item.status = "resolved"
                item.data = {**item.data, "resolution": "Official source verified", "resolved_at": now_iso()}
            audit(session, application.id, "listing_verified", availability=record.availability,
                  evidence=result.get("evidence", {}), snapshot_hash=snapshot_hash)
        else:
            task(session, f"verify:{identity}", "Verify an official employer record", "listing_unverified",
                 application=application, context={"url": record.url})
        return opportunity_view(session, record)


def import_email_alert(db, raw_email):
    message = Parser(policy=policy.default).parsestr(raw_email)
    body = message.get_body(preferencelist=("html", "plain")) if message.is_multipart() else message
    content = body.get_content() if body else ""
    soup = BeautifulSoup(content, "html.parser")
    import re
    candidates = [(a.get("href", ""), a.get_text(" ", strip=True)) for a in soup.select("a[href]")]
    candidates += [(x, "Imported email alert lead") for x in re.findall(r"https://[^\s<>\"]+", soup.get_text(" "))]
    source = register_source(db, {"name": "Owner imported email alerts", "connector": "email_alert"})
    occurrences = []
    for url, title in candidates[:100]:
        try:
            url = canonical_url(url.rstrip(".,)"))
        except ValueError:
            continue
        occurrences.append({"external_id": digest(url), "title": title[:500] or "Email alert lead",
            "employer": urlsplit(url).hostname or "Unverified employer", "url": url, "description": "",
            "is_primary": False, "availability": "unverified", "source_posted_at": None,
            "raw_snapshot": {"subject": str(message.get("Subject", "")), "url": url}, "origin": "email_alert"})
    return {"imported": len(ingest_occurrences(db, source["id"], occurrences)), "status": "unverified_leads"}
