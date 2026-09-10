"""Normalization of public listings and narrowly scoped primary verification."""
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from app.connectors.http import ConnectorError, FetchResponse, network_url, retry_after, safe_fetch
from app.security.urls import canonical_url

BOARD = re.compile(r"[A-Za-z0-9_-]{1,150}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,150}\Z")
API_HOSTS = {
    "greenhouse": frozenset({"boards-api.greenhouse.io"}),
    "lever": frozenset({"api.lever.co", "api.eu.lever.co"}),
    "ashby": frozenset({"api.ashbyhq.com"}),
    "cambridge": frozenset({"www.jobs.cam.ac.uk"}),
}
READ_REVIEWS = {
    "greenhouse": "https://docs.greenhouse.io/job-board.html",
    "lever": "https://github.com/lever/postings-api",
    "ashby": "https://developers.ashbyhq.com/docs/public-job-posting-api",
    "cambridge": "https://www.jobs.cam.ac.uk/data/",
}
COUNTRIES = {"United Kingdom": "GB", "UK": "GB", "GBR": "GB", "United States": "US", "USA": "US",
    "France": "FR", "FRA": "FR", "Canada": "CA", "CAN": "CA", "Germany": "DE", "DEU": "DE",
    "Morocco": "MA", "MAR": "MA", "Netherlands": "NL", "NLD": "NL", "Spain": "ES", "ESP": "ES",
    "Belgium": "BE", "BEL": "BE", "Switzerland": "CH", "CHE": "CH", "Ireland": "IE", "IRL": "IE",
    "Italy": "IT", "ITA": "IT", "Portugal": "PT", "PRT": "PT", "Sweden": "SE", "SWE": "SE"}


def _source(source):
    if not isinstance(source, dict):
        raise ConnectorError("invalid_source")
    result = {**source.get("data", {}), **source}
    if result.get("enabled") is False:
        raise ConnectorError("source_disabled")
    if result.get("connector") not in {*API_HOSTS, "rss", "html", "email_alert", "search"}:
        raise ConnectorError("unsupported_connector")
    return result


def _board(source):
    value = source.get("board_id", "")
    if not isinstance(value, str) or not BOARD.fullmatch(value):
        raise ConnectorError("invalid_board_id")
    return value


def _id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ConnectorError("invalid_record_id")
    return value


def _plain(value):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 500000:
        raise ConnectorError("invalid_record_text")
    value = html.unescape(html.unescape(value))
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.find_all(["script", "style", "iframe", "object", "embed", "template"]):
        node.decompose()
    return soup.get_text(" ", strip=True)


def _country(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return "GB" if value.upper() == "UK" else value.upper()
    return COUNTRIES.get(value, "")


def _date(value):
    """Normalize explicit instants only; never add a cutoff to a date-only value."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return stamp.astimezone(UTC).isoformat()


def _track(title, source):
    if source.get("track") in {"data_ai", "software", "data_bi", "academic", "income_only"}:
        return source["track"]
    if source["connector"] == "cambridge" or re.search(r"\b(phd|doctoral|postdoc|research)\b", title, re.I):
        return "academic"
    if re.search(r"\b(analyst|business intelligence|power bi)\b", title, re.I):
        return "data_bi"
    if re.search(r"\b(software|backend|frontend|devops|fullstack)\b", title, re.I):
        return "software"
    return "data_ai"


def _record(source, raw, *, identity, title, url, description="", employer="", country="", posted=None,
            deadline=None, primary=True, authoritative_key=None, application_url="", location="",
            employment_type="", language=None, evidence=None):
    if not isinstance(raw, dict):
        raise ConnectorError("invalid_record")
    title, description = _plain(title), _plain(description)
    if not title or len(title) > 500:
        raise ConnectorError("invalid_record_title")
    try:
        url = canonical_url(url)
        application_url = canonical_url(application_url) if application_url else ""
    except (ValueError, TypeError):
        raise ConnectorError("unsafe_record_url") from None
    language = language if language in {"en", "fr"} else source.get("language", "en")
    if language not in {"en", "fr"}:
        language = "en"
    stamp = datetime.now(UTC).isoformat()
    parsed_deadline = _date(deadline)
    availability = "open" if primary else "unverified"
    if parsed_deadline and parsed_deadline <= stamp:
        availability = "closed" if primary else "unverified"
    return {"external_id": str(identity), "authoritative_key": authoritative_key, "title": title,
        "employer": _plain(employer or source.get("employer") or source.get("name") or source.get("board_id", "")),
        "url": url, "description": description, "country": _country(country), "track": _track(title, source),
        "language": language, "availability": availability, "verified_at": stamp if primary else None,
        "source_posted_at": _date(posted) or (posted if isinstance(posted, str) else None),
        "deadline": parsed_deadline, "deadline_text": str(deadline or ""),
        "deadline_timezone": "explicit_offset" if parsed_deadline else None,
        "is_primary": primary, "raw_snapshot": raw, "route": "manual", "application_url": application_url,
        "location": _plain(location), "employment_type": _plain(employment_type),
        "evidence": {"kind": "published_primary_api" if primary else "discovery_lead",
            "connector": source["connector"], "review_url": READ_REVIEWS.get(source["connector"]),
            "read_at": stamp, "source_url": source.get("url", ""), "observed_id": str(identity),
            "deadline_requires_review": bool(deadline and not parsed_deadline), **(evidence or {})}}


def _greenhouse(source, raw):
    identity, board = _id(raw.get("id")), _board(source)
    internal = raw.get("internal_job_id")
    key = f"greenhouse:{board}:job:{_id(internal)}" if internal is not None else f"greenhouse:{board}:post:{identity}"
    return _record(source, raw, identity=identity, title=raw.get("title"), url=raw.get("absolute_url"),
        employer=raw.get("company_name", ""), description=raw.get("content", ""),
        country=raw.get("country", ""), location=(raw.get("location") or {}).get("name", ""),
        posted=raw.get("first_published"), deadline=raw.get("application_deadline"),
        authoritative_key=key, primary=internal is not None, language=raw.get("language"),
        evidence={"requisition_id": raw.get("requisition_id"), "updated_at": raw.get("updated_at"),
                  "prospect_post": internal is None})


def _lever(source, raw):
    identity, board = _id(raw.get("id")), _board(source)
    description = raw.get("descriptionPlain") or raw.get("description", "")
    for item in raw.get("lists", []):
        description += "\n" + _plain(item.get("text", "")) + ": " + _plain(item.get("content", ""))
    description += "\n" + (raw.get("additionalPlain") or raw.get("additional", ""))
    categories = raw.get("categories") or {}
    return _record(source, raw, identity=identity, title=raw.get("text"), url=raw.get("hostedUrl"),
        description=description, country=raw.get("country"), location=categories.get("location", ""),
        employment_type=categories.get("commitment", ""), application_url=raw.get("applyUrl", ""),
        authoritative_key=f"lever:{source.get('region', 'global')}:{board}:{identity}",
        evidence={"workplace_type": raw.get("workplaceType"), "all_locations": categories.get("allLocations", [])})


def _ashby(source, raw):
    board = _board(source)
    identity = _id(raw.get("id") or urlsplit(raw.get("jobUrl", "")).path.rstrip("/").split("/")[-1])
    address = ((raw.get("address") or {}).get("postalAddress") or {})
    return _record(source, raw, identity=identity, title=raw.get("title"), url=raw.get("jobUrl"),
        description=raw.get("descriptionPlain") or raw.get("descriptionHtml", ""),
        country=address.get("addressCountry", ""), posted=raw.get("publishedAt"),
        location=raw.get("location", ""), employment_type=raw.get("employmentType", ""),
        application_url=raw.get("applyUrl", ""), authoritative_key=f"ashby:{board}:{identity}",
        evidence={"workplace_type": raw.get("workplaceType"), "is_remote": raw.get("isRemote"),
                  "secondary_locations": raw.get("secondaryLocations", []), "is_listed": raw.get("isListed")})


def _cambridge(source, raw, *, primary=False):
    identity = _id(raw.get("id"))
    dates, advert = raw.get("dates") or {}, raw.get("advert") or {}
    return _record(source, raw, identity=identity, title=raw.get("title"), url=raw.get("url"),
        description=advert.get("text") or advert.get("html", ""), employer="University of Cambridge",
        country=raw.get("country", ""), posted=dates.get("published"), deadline=dates.get("closes"),
        authoritative_key=f"cambridge:{raw.get('reference') or identity}", primary=primary, language="en",
        application_url=raw.get("applyOnlineUrl", ""),
        evidence={"job_reference": raw.get("reference"), "category": raw.get("category"),
                  "department": raw.get("unit"), "salary": raw.get("salary")})


def source_endpoint(source):
    source = _source(source)
    connector = source["connector"]
    if connector == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{_board(source)}/jobs?content=true"
    if connector == "lever":
        if source.get("region", "global") not in {"global", "eu"}:
            raise ConnectorError("invalid_region")
        host = "api.eu.lever.co" if source.get("region") == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{_board(source)}?mode=json"
    if connector == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{_board(source)}"
    if connector == "cambridge":
        return "https://www.jobs.cam.ac.uk/job/?category=2&format=json"
    if connector in {"rss", "html"}:
        if source.get("permission_status") != "permitted" or not source.get("permission_note"):
            raise ConnectorError("permission_required", "Review and record this site's permitted read access")
        return network_url(source.get("url", ""))
    raise ConnectorError("unconfigured_connection", "Import alerts manually or configure a licensed search provider")


def _request(fetcher, url, hosts, headers=None):
    response = (fetcher or safe_fetch)(url, allowed_hosts=hosts, headers=headers or {})
    if not isinstance(response, FetchResponse):
        raise ConnectorError("invalid_fetch_response")
    lowered = {key.lower(): value for key, value in response.headers.items()}
    if response.status == 429:
        raise ConnectorError("rate_limited", retry_after_seconds=retry_after(lowered.get("retry-after")), status=429)
    if response.status >= 500:
        raise ConnectorError("source_unavailable", status=response.status)
    if response.status not in {200, 304, 404, 410}:
        raise ConnectorError("http_error", status=response.status)
    if len(response.body) > 8_000_000:
        raise ConnectorError("response_too_large")
    return FetchResponse(response.status, response.body, lowered, response.url or url)


def _json(response):
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectorError("invalid_json") from None


def _result(response, occurrences=(), *, cursor=None, complete=True):
    return {"occurrences": list(occurrences), "cursor": cursor, "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"), "not_modified": response.status == 304,
            "complete": complete}


def _conditional(source):
    return {key: str(value) for key, value in (("If-None-Match", source.get("etag")),
            ("If-Modified-Since", source.get("last_modified"))) if value}


def _feed_records(source, response):
    try:
        root = ElementTree.fromstring(response.body)
    except Exception:
        raise ConnectorError("invalid_feed", "Feed XML is malformed or contains forbidden entities") from None
    items = root.findall("./channel/item") + root.findall("{http://www.w3.org/2005/Atom}entry")
    if len(items) > 5000:
        raise ConnectorError("too_many_records")
    records = []
    for item in items:
        atom = item.tag.endswith("entry")
        prefix = "{http://www.w3.org/2005/Atom}" if atom else ""
        def text(name):
            return item.findtext(prefix + name) or ""
        link = item.find(prefix + "link")
        url = (link.get("href", "") if atom and link is not None else text("link")).strip()
        url = urljoin(response.url, url)
        identity = text("id" if atom else "guid") or url
        records.append(_record(source, {"title": text("title"), "url": url,
            "description": text("content" if atom else "description"), "id": identity}, identity=identity,
            title=text("title"), url=url, description=text("content" if atom else "description") or text("summary"),
            posted=text("published" if atom else "pubDate") or text("updated"), primary=False))
    return records


def fetch_source(source: dict, fetcher=None) -> dict:
    source = _source(source)
    endpoint = source_endpoint(source)
    connector = source["connector"]
    hosts = API_HOSTS.get(connector, frozenset({urlsplit(endpoint).hostname}))
    if connector == "lever":
        return _fetch_lever(source, endpoint, hosts, fetcher)
    response = _request(fetcher, endpoint, hosts, _conditional(source))
    if response.status == 304:
        return _result(response)
    if response.status in {404, 410}:
        raise ConnectorError("source_not_found", status=response.status)
    if connector == "rss":
        return _result(response, _feed_records(source, response))
    if connector == "html":
        return _result(response, _html_records(source, response, primary=False))
    body = _json(response)
    if not isinstance(body, dict):
        raise ConnectorError("invalid_response")
    if connector == "cambridge":
        content = body.get("list")
        if not isinstance(content, dict) or "jobs" not in content:
            raise ConnectorError("invalid_response")
        jobs = content["jobs"]
        if isinstance(jobs, dict):
            jobs = jobs.get("job", [])
            if isinstance(jobs, dict):
                jobs = [jobs]
    else:
        jobs = body.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > 10000 or any(not isinstance(job, dict) for job in jobs):
        raise ConnectorError("invalid_response")
    normalizer = {"greenhouse": _greenhouse, "ashby": _ashby, "cambridge": _cambridge}[connector]
    records = []
    for job in jobs:
        if connector == "ashby":
            if type(job.get("isListed")) is not bool:
                raise ConnectorError("invalid_record", "Ashby listing visibility field is missing or malformed")
            if job["isListed"] is False:
                continue
        try:
            records.append(normalizer(source, job))
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ConnectorError("invalid_record") from None
    expected = (body.get("meta") or {}).get("total", len(jobs))
    complete = type(expected) is int and expected == len(jobs)
    return _result(response, records, complete=complete)


def _fetch_lever(source, endpoint, hosts, fetcher):
    offset = source.get("cursor") or 0
    if isinstance(offset, str) and offset.isdecimal():
        offset = int(offset)
    limit, page_cap = source.get("page_size", 100), source.get("max_pages", 5)
    if (type(offset) is not int or not 0 <= offset <= 100000 or type(limit) is not int
            or not 1 <= limit <= 100 or type(page_cap) is not int or not 1 <= page_cap <= 20):
        raise ConnectorError("invalid_pagination")
    records, seen, first = [], set(), None
    for page in range(page_cap):
        response = _request(fetcher, f"{endpoint}&skip={offset}&limit={limit}", hosts,
                            _conditional(source) if offset == 0 and page == 0 else {})
        first = first or response
        if response.status == 304:
            if offset:
                raise ConnectorError("invalid_pagination_response")
            return _result(response)
        if response.status in {404, 410}:
            raise ConnectorError("source_not_found", status=response.status)
        jobs = _json(response)
        if not isinstance(jobs, list) or len(jobs) > limit or any(not isinstance(job, dict) for job in jobs):
            raise ConnectorError("invalid_response")
        for job in jobs:
            try:
                record = _lever(source, job)
            except (AttributeError, TypeError, KeyError, ValueError):
                raise ConnectorError("invalid_record") from None
            if record["external_id"] in seen:
                raise ConnectorError("unstable_pagination", "Repeated posting across pages; retry a fresh scan")
            seen.add(record["external_id"])
            records.append(record)
        offset += len(jobs)
        if len(jobs) < limit:
            return _result(first, records)
    return _result(first, records, cursor=offset, complete=False)


def _jsonld_jobs(soup):
    jobs = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            stack = [json.loads(script.string or script.get_text())]
        except (ValueError, TypeError, RecursionError):
            continue
        visited = 0
        while stack:
            visited += 1
            if visited > 10000:
                raise ConnectorError("structured_data_too_large")
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, dict):
                kind = value.get("@type")
                if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                    jobs.append(value)
                if "@graph" in value:
                    stack.append(value["@graph"])
    if len(jobs) > 1000:
        raise ConnectorError("too_many_records")
    return jobs


def _html_records(source, response, *, primary):
    soup = BeautifulSoup(response.body, "html.parser")
    jobs = _jsonld_jobs(soup)
    records = []
    for job in jobs:
        url = job.get("url") or job.get("@id")
        if not url and len(jobs) == 1:
            url = response.url
        if not isinstance(url, str):
            raise ConnectorError("ambiguous_primary_listing")
        url = urljoin(response.url, url)
        identity = job.get("identifier")
        if isinstance(identity, dict):
            identity = identity.get("value")
        identity = str(identity) if isinstance(identity, (str, int)) else hashlib.sha256(url.encode()).hexdigest()
        organization = job.get("hiringOrganization") or {}
        employer = organization.get("name", "") if isinstance(organization, dict) else organization
        locations = job.get("jobLocation") or []
        locations = locations if isinstance(locations, list) else [locations]
        countries = set()
        location_names = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address") or {}
            if isinstance(address, dict):
                country = address.get("addressCountry", "")
                if isinstance(country, dict):
                    country = country.get("name", "")
                country = _country(country)
                if country:
                    countries.add(country)
                location_names.append(str(address.get("addressLocality", "")))
        host = urlsplit(response.url).hostname
        records.append(_record(source, job, identity=identity, title=job.get("title"), url=url,
            description=job.get("description", ""), employer=employer, primary=primary,
            posted=job.get("datePosted"), deadline=job.get("validThrough"),
            country=next(iter(countries)) if len(countries) == 1 else "",
            location=", ".join(location_names), authoritative_key=f"primary:{host}:{identity}",
            employment_type=", ".join(job["employmentType"]) if isinstance(job.get("employmentType"), list)
                else job.get("employmentType", ""),
            evidence={"kind": "primary_structured_data" if primary else "discovery_lead",
                "job_location_type": job.get("jobLocationType"), "applicant_location_requirements":
                job.get("applicantLocationRequirements"), "verification_url": response.url}))
    return records


def _infer_source(opportunity):
    """Recognize only documented hosted-board URL structures, never arbitrary domains."""
    parsed = urlsplit(canonical_url(opportunity.get("url", "")))
    parts = parsed.path.strip("/").split("/")
    common = {"name": opportunity.get("employer", ""), "permission_status": "permitted",
              "language": opportunity.get("language", "en")}
    if parsed.hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and len(parts) == 3 and parts[1] == "jobs":
        return {**common, "connector": "greenhouse", "board_id": parts[0], "url": opportunity["url"]}, parts[2]
    if parsed.hostname in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) == 2:
        return {**common, "connector": "lever", "board_id": parts[0], "url": opportunity["url"],
                "region": "eu" if parsed.hostname == "jobs.eu.lever.co" else "global"}, parts[1]
    if parsed.hostname == "jobs.ashbyhq.com" and len(parts) == 2:
        return {**common, "connector": "ashby", "board_id": parts[0], "url": opportunity["url"]}, parts[1]
    if parsed.hostname == "www.jobs.cam.ac.uk" and len(parts) == 2 and parts[0] == "job":
        return {**common, "connector": "cambridge", "url": opportunity["url"]}, parts[1]
    raise ConnectorError("permission_required", "Register and review the primary site's permitted access first")


def _unavailable(opportunity, response, source, reason="primary_listing_removed"):
    # An HTTP failure from a board listing endpoint must never reach this function.
    return {**opportunity, "external_id": str(opportunity.get("external_id", "")),
            "availability": "closed", "verified_at": datetime.now(UTC).isoformat(), "is_primary": True,
            "raw_snapshot": {"status": response.status, "url": response.url},
            "evidence": {"kind": reason, "http_status": response.status,
                         "verification_url": response.url, "connector": source["connector"]}}


def verify_listing(opportunity: dict, source: dict | None = None, fetcher=None) -> dict:
    if not isinstance(opportunity, dict):
        raise ConnectorError("invalid_opportunity")
    opportunity = {**opportunity.get("data", {}), **opportunity}
    if source is None:
        source, identity = _infer_source(opportunity)
    else:
        source = _source(source)
        identity = opportunity.get("external_id")
        if not identity and source["connector"] in API_HOSTS:
            inferred, identity = _infer_source(opportunity)
            if inferred["connector"] != source["connector"] or inferred.get("board_id") != source.get("board_id"):
                raise ConnectorError("source_identity_mismatch")
    connector = source["connector"]
    endpoint = source_endpoint(source)
    if connector == "ashby":
        # The public Ashby API has no documented individual-post GET. Re-read the
        # complete board; a failed/incomplete response does not establish closure.
        response = _request(fetcher, endpoint, API_HOSTS[connector])
        if response.status != 200:
            raise ConnectorError("incomplete_verification")
        body = _json(response)
        jobs = body.get("jobs") if isinstance(body, dict) else None
        if (not isinstance(jobs, list) or len(jobs) > 10000
                or any(not isinstance(job, dict) or type(job.get("isListed")) is not bool for job in jobs)):
            raise ConnectorError("invalid_response")
        for raw in jobs:
            record = _ashby(source, raw)
            if record["external_id"] != str(identity):
                continue
            if raw["isListed"] is False:
                record.update(availability="unverified", is_primary=False, verified_at=None)
                record["evidence"]["kind"] = "unlisted_primary_post_requires_review"
            return record
        return _unavailable(opportunity, FetchResponse(200, url=endpoint), source, "absent_from_complete_primary_board")
    if connector in {"greenhouse", "lever", "cambridge"}:
        identity = _id(identity)
        if connector == "greenhouse":
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{_board(source)}/jobs/{identity}"
        elif connector == "lever":
            endpoint = endpoint.split("?", 1)[0] + "/" + identity + "?mode=json"
        else:
            endpoint = f"https://www.jobs.cam.ac.uk/job/{identity}/?format=json"
        response = _request(fetcher, endpoint, API_HOSTS[connector])
        if response.status in {404, 410}:
            return _unavailable(opportunity, response, source)
        if response.status != 200:
            raise ConnectorError("incomplete_verification")
        raw = _json(response)
        if connector == "cambridge" and isinstance(raw, dict):
            raw = raw.get("job", raw)
        if not isinstance(raw, dict) or str(raw.get("id")) != identity:
            raise ConnectorError("record_identity_mismatch")
        try:
            record = _cambridge(source, raw, primary=True) if connector == "cambridge" else (
                _greenhouse(source, raw) if connector == "greenhouse" else _lever(source, raw))
        except (AttributeError, TypeError, KeyError, ValueError):
            raise ConnectorError("invalid_record") from None
        record["evidence"]["verification_url"] = response.url
        return record
    if connector in {"rss", "html"}:
        # A reviewed discovery aggregator cannot grant access to arbitrary linked
        # employers. The registry must mark this exact host as a primary source.
        if source.get("is_primary") is not True:
            raise ConnectorError("primary_source_required")
        url = network_url(opportunity.get("url", ""))
        host = urlsplit(endpoint).hostname
        if urlsplit(url).hostname != host:
            raise ConnectorError("primary_source_required")
        response = _request(fetcher, url, frozenset({host}))
        if response.status in {404, 410}:
            return _unavailable(opportunity, response, source)
        if response.status != 200:
            raise ConnectorError("incomplete_verification")
        matches = [record for record in _html_records(source, response, primary=True)
                   if record["url"] == canonical_url(url)]
        if len(matches) == 1:
            return matches[0]
        return {**opportunity, "availability": "unverified", "verified_at": None, "is_primary": False,
                "raw_snapshot": {"url": response.url, "body_sha256": hashlib.sha256(response.body).hexdigest()},
                "evidence": {"kind": "primary_evidence_inconclusive", "verification_url": response.url}}
    raise ConnectorError("unsupported_verification")
