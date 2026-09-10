import copy
import gzip
import io
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.connectors import ConnectorError, FetchResponse, fetch_source, verify_listing
from app.connectors.http import SafeFetcher, _PinnedHTTPSConnection, public_addresses, retry_after
from app.connectors.sources import source_endpoint

FIXTURES = Path(__file__).parents[1] / "fixtures" / "discovery"


def fixture(name):
    return (FIXTURES / name).read_bytes()


def source(connector, **extra):
    return {"connector": connector, "board_id": "fixture", "name": "Fixture Employer",
            "permission_status": "permitted", **extra}


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, *, allowed_hosts, headers):
        self.calls.append({"url": url, "hosts": allowed_hosts, "headers": headers})
        return self.responses.pop(0)


def response(name, **headers):
    return FetchResponse(200, fixture(name), headers)


def test_greenhouse_authoritative_identity_and_literal_evidence():
    result = fetch_source(source("greenhouse"), Responses(response("greenhouse.json")))
    assert result["complete"] is True
    first, second = result["occurrences"]
    assert first["authoritative_key"] == second["authoritative_key"] == "greenhouse:fixture:job:500"
    assert first["external_id"] != second["external_id"]
    assert first["availability"] == "open" and first["is_primary"] is True
    assert first["source_posted_at"] == "2026-09-01T10:00:00+00:00"
    assert first["country"] == ""  # A city string does not silently decide eligibility.
    assert "stealCookies" not in first["description"]
    assert second["language"] == "fr"


def test_greenhouse_update_timestamp_is_not_publication_time():
    raw = json.loads(fixture("greenhouse.json"))
    del raw["jobs"][0]["first_published"]
    result = fetch_source(source("greenhouse"), Responses(FetchResponse(200, json.dumps(raw).encode())))
    assert result["occurrences"][0]["source_posted_at"] is None


def test_greenhouse_prospect_post_remains_unverified():
    raw = json.loads(fixture("greenhouse.json"))
    raw["jobs"][0]["internal_job_id"] = None
    result = fetch_source(source("greenhouse"), Responses(FetchResponse(200, json.dumps(raw).encode())))
    assert result["occurrences"][0]["availability"] == "unverified"


def test_lever_pages_keep_requirements_and_only_publisher_country():
    first = json.loads(fixture("lever.json"))
    second = copy.deepcopy(first)
    second[0]["id"] = "fixture-post-2"
    fetcher = Responses(FetchResponse(200, json.dumps(first).encode()),
                        FetchResponse(200, json.dumps(second).encode()), FetchResponse(200, b"[]"))
    result = fetch_source(source("lever", page_size=1), fetcher)
    assert result["complete"] and result["cursor"] is None
    assert len(result["occurrences"]) == 2
    assert "Master's degree required" in result["occurrences"][0]["description"]
    assert result["occurrences"][0]["country"] == "FR"
    assert [call["url"].split("skip=")[1].split("&")[0] for call in fetcher.calls] == ["0", "1", "2"]


def test_bounded_lever_pages_expose_resumable_cursor():
    fetcher = Responses(response("lever.json"))
    result = fetch_source(source("lever", page_size=1, max_pages=1), fetcher)
    assert not result["complete"] and result["cursor"] == 1
    resume = Responses(FetchResponse(200, b"[]"))
    assert fetch_source(source("lever", page_size=1, cursor=result["cursor"]), resume)["complete"]
    assert "skip=1" in resume.calls[0]["url"]


def test_lever_eu_route_and_repeated_page_detection():
    fetcher = Responses(response("lever.json"), response("lever.json"))
    with pytest.raises(ConnectorError, match="Repeated") as error:
        fetch_source(source("lever", region="eu", page_size=1), fetcher)
    assert error.value.code == "unstable_pagination"
    assert fetcher.calls[0]["url"].startswith("https://api.eu.lever.co/")


@pytest.mark.parametrize("values", [{"cursor": -1}, {"cursor": "../../../"}, {"max_pages": True}, {"page_size": 101}])
def test_invalid_pagination_never_fetches(values):
    fetcher = Responses()
    with pytest.raises(ConnectorError):
        fetch_source(source("lever", **values), fetcher)
    assert not fetcher.calls


def test_ashby_hides_unlisted_posts_and_keeps_unknown_region():
    result = fetch_source(source("ashby"), Responses(response("ashby.json")))
    assert len(result["occurrences"]) == 1
    assert result["occurrences"][0]["country"] == ""
    assert result["occurrences"][0]["evidence"]["is_remote"] is True


def test_cambridge_list_is_lead_and_date_only_deadline_needs_review():
    result = fetch_source(source("cambridge"), Responses(response("cambridge.json")))
    record = result["occurrences"][0]
    assert record["track"] == "academic" and record["availability"] == "unverified"
    assert record["deadline"] is None and record["deadline_text"] == "2099-11-30"
    assert record["deadline_timezone"] is None and record["evidence"]["deadline_requires_review"]
    assert record["source_posted_at"] == "2026-09-01"


def test_cambridge_primary_job_verifies_availability_only():
    raw = json.loads(fixture("cambridge.json"))["list"]["jobs"][0]
    fetcher = Responses(FetchResponse(200, json.dumps({"job": raw}).encode()))
    result = verify_listing({"url": raw["url"]}, fetcher=fetcher)
    assert result["availability"] == "open" and result["is_primary"]
    assert result["deadline"] is None and result["country"] == ""
    assert fetcher.calls[0]["url"] == "https://www.jobs.cam.ac.uk/job/12345/?format=json"


def test_feed_only_imports_unverified_leads_without_script_text():
    source_data = source("rss", url="https://research.example/feed", permission_note="Fixture permission review")
    result = fetch_source(source_data, Responses(response("alerts.xml")))
    record = result["occurrences"][0]
    assert not record["is_primary"] and record["verified_at"] is None
    assert "ignoreAllRules" not in record["description"]
    assert record["authoritative_key"] is None


def test_feed_entities_are_rejected():
    malicious = b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>&x;</channel></rss>'
    with pytest.raises(ConnectorError) as error:
        fetch_source(source("rss", url="https://research.example/feed", permission_note="Fixture reviewed"),
                     Responses(FetchResponse(200, malicious)))
    assert error.value.code == "invalid_feed"


@pytest.mark.parametrize("connector", ["rss", "html"])
def test_generic_sources_require_permission_before_network(connector):
    fetcher = Responses()
    with pytest.raises(ConnectorError) as error:
        fetch_source(source(connector, url="https://employer.example/jobs"), fetcher)
    assert error.value.code == "permission_required" and not fetcher.calls


def test_html_structured_primary_evidence_and_discovery_are_distinct():
    config = source("html", url="https://employer.example/jobs", permission_note="Fixture reviewed", is_primary=True)
    discovered = fetch_source(config, Responses(response("primary.html")))["occurrences"][0]
    assert discovered["availability"] == "unverified"
    result = verify_listing({"url": "https://employer.example/jobs/42"}, config, Responses(response("primary.html")))
    assert result["availability"] == "open" and result["country"] == "FR"
    assert result["evidence"]["kind"] == "primary_structured_data"


def test_generic_plain_html_never_proves_open_listing():
    config = source("html", url="https://employer.example/jobs", permission_note="Fixture reviewed", is_primary=True)
    result = verify_listing({"url": "https://employer.example/jobs/42"}, config,
                            Responses(FetchResponse(200, b"<p>We are hiring!</p>")))
    assert result["availability"] == "unverified" and result["verified_at"] is None


def test_aggregator_cannot_follow_arbitrary_employer_links():
    config = source("rss", url="https://aggregator.example/feed", permission_note="Fixture reviewed", is_primary=True)
    with pytest.raises(ConnectorError) as error:
        verify_listing({"url": "https://other.example/jobs/42"}, config, Responses())
    assert error.value.code == "primary_source_required"


def test_http_404_on_primary_detail_is_closed_but_board_404_is_error():
    opportunity = {"url": "https://job-boards.greenhouse.io/fixture/jobs/101"}
    assert verify_listing(opportunity, fetcher=Responses(FetchResponse(404)))["availability"] == "closed"
    with pytest.raises(ConnectorError) as error:
        fetch_source(source("greenhouse"), Responses(FetchResponse(404)))
    assert error.value.code == "source_not_found"


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (500, "source_unavailable"), (503, "source_unavailable")])
def test_outages_never_produce_closure(status, code):
    opportunity = {"url": "https://jobs.lever.co/fixture/fixture-post-1"}
    with pytest.raises(ConnectorError) as error:
        verify_listing(opportunity, fetcher=Responses(FetchResponse(status, headers={"Retry-After": "120"})))
    assert error.value.code == code
    if status == 429:
        assert error.value.retry_after_seconds == 120


def test_conditional_not_modified_preserves_empty_delta_semantics():
    fetcher = Responses(FetchResponse(304, headers={"ETag": '"fixture-tag"'}))
    result = fetch_source(source("greenhouse", etag='"fixture-tag"'), fetcher)
    assert result["not_modified"] and not result["occurrences"]
    assert fetcher.calls[0]["headers"] == {"If-None-Match": '"fixture-tag"'}
    assert result["etag"] == '"fixture-tag"'


def test_incomplete_response_must_not_close_missing_jobs():
    raw = json.loads(fixture("greenhouse.json"))
    raw["meta"]["total"] = 50
    result = fetch_source(source("greenhouse"), Responses(FetchResponse(200, json.dumps(raw).encode())))
    assert not result["complete"]


def test_unknown_ashby_id_closed_only_after_successful_complete_primary_scan():
    result = verify_listing({"url": "https://jobs.ashbyhq.com/fixture/gone"},
                            fetcher=Responses(response("ashby.json")))
    assert result["availability"] == "closed"
    assert result["evidence"]["kind"] == "absent_from_complete_primary_board"


def test_ashby_unlisted_direct_link_does_not_mean_closed():
    result = verify_listing({"url": "https://jobs.ashbyhq.com/fixture/unlisted-fixture"},
                            fetcher=Responses(response("ashby.json")))
    assert result["availability"] == "unverified" and result["verified_at"] is None
    assert result["evidence"]["kind"] == "unlisted_primary_post_requires_review"


def test_ashby_missing_visibility_is_schema_error_not_empty_board():
    raw = json.loads(fixture("ashby.json"))
    del raw["jobs"][0]["isListed"]
    body = json.dumps(raw).encode()
    with pytest.raises(ConnectorError):
        fetch_source(source("ashby"), Responses(FetchResponse(200, body)))
    with pytest.raises(ConnectorError):
        verify_listing({"url": "https://jobs.ashbyhq.com/fixture/fixture-ashby-1"},
                       fetcher=Responses(FetchResponse(200, body)))


def test_detail_identity_mismatch_is_not_success():
    raw = json.loads(fixture("greenhouse.json"))["jobs"][0]
    with pytest.raises(ConnectorError) as error:
        verify_listing({"url": "https://job-boards.greenhouse.io/fixture/jobs/999"},
                       fetcher=Responses(FetchResponse(200, json.dumps(raw).encode())))
    assert error.value.code == "record_identity_mismatch"


@pytest.mark.parametrize("board", ["../attacker", "fixture?x=1", "", "foo.bar", " fixture", None])
def test_board_id_is_exact_validated_path_segment(board):
    with pytest.raises(ConnectorError):
        source_endpoint(source("greenhouse", board_id=board))


@pytest.mark.parametrize("body", [b"<html>source error</html>", b"[]", b'{"jobs":"wrong"}'])
def test_malformed_api_response_is_not_empty_complete_scan(body):
    with pytest.raises(ConnectorError):
        fetch_source(source("greenhouse"), Responses(FetchResponse(200, body)))


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


class WireResponse:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.body, self.status, self.headers = io.BytesIO(body), status, headers or {}

    def read(self, length):
        return self.body.read(length)

    def getheaders(self):
        return list(self.headers.items())

    def close(self):
        pass


class Connections:
    def __init__(self, *responses):
        self.responses, self.destinations, self.requests = list(responses), [], []

    def __call__(self, host, address, timeout):
        self.destinations.append((host, address, timeout))
        owner = self
        class Connection:
            def request(self, *args, **kwargs):
                owner.requests.append((args, kwargs))

            def getresponse(self):
                return owner.responses.pop(0)

            def close(self):
                pass
        return Connection()


def test_public_client_pins_checked_ip_and_performs_only_get():
    connections = Connections(WireResponse())
    fetcher = SafeFetcher(resolver=public_dns, connection_factory=connections)
    result = fetcher("https://employer.example/jobs/", allowed_hosts=frozenset({"employer.example"}))
    assert result.status == 200
    assert connections.destinations[0][1] == (socket.AF_INET, ("93.184.216.34", 443))
    assert connections.requests[0][0] == ("GET", "/jobs/")


def test_pinned_tls_preserves_hostname_sni_without_second_dns_lookup(monkeypatch):
    connection = _PinnedHTTPSConnection("employer.example", (socket.AF_INET, ("93.184.216.34", 443)), 5)
    raw, context = Mock(), Mock()
    connection._context = context
    monkeypatch.setattr(socket, "socket", lambda *args: raw)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: pytest.fail("Unexpected second DNS lookup"))
    connection.connect()
    raw.connect.assert_called_once_with(("93.184.216.34", 443))
    context.wrap_socket.assert_called_once_with(raw, server_hostname="employer.example")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.2", "224.0.0.1", "0.0.0.0"])
def test_private_resolved_addresses_are_rejected(address):
    def resolver(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]
    with pytest.raises(ConnectorError) as error:
        public_addresses("employer.example", resolver)
    assert error.value.code == "unsafe_address"


def test_mixed_public_and_private_dns_answers_fail_closed():
    def resolver(*args, **kwargs):
        return public_dns(None, None) + [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))]
    with pytest.raises(ConnectorError):
        public_addresses("employer.example", resolver)


def test_redirect_to_nonallowlisted_host_never_resolves_it():
    connections = Connections(WireResponse(status=302, headers={"Location": "https://other.example/private"}))
    fetcher = SafeFetcher(resolver=public_dns, connection_factory=connections)
    with pytest.raises(ConnectorError) as error:
        fetcher("https://employer.example/jobs", allowed_hosts=frozenset({"employer.example"}))
    assert error.value.code == "host_not_permitted" and len(connections.destinations) == 1


def test_same_host_redirect_rechecks_and_pins_new_address():
    connections = Connections(WireResponse(status=301, headers={"Location": "/jobs/"}), WireResponse())
    fetcher = SafeFetcher(resolver=public_dns, connection_factory=connections)
    assert fetcher("https://employer.example/jobs", allowed_hosts=frozenset({"employer.example"})).status == 200
    assert len(connections.destinations) == 2


@pytest.mark.parametrize("url", ["http://employer.example", "https://127.0.0.1/", "https://employer.example:8080/",
                                "https://user:pass@employer.example", "https://2130706433/", "https://x.localhost/"])
def test_unsafe_urls_fail_before_connection(url):
    connections = Connections()
    with pytest.raises(ConnectorError):
        SafeFetcher(resolver=public_dns, connection_factory=connections)(url, allowed_hosts=frozenset({"employer.example"}))
    assert not connections.destinations


def test_gzip_expansion_is_bounded_before_large_allocation():
    connections = Connections(WireResponse(gzip.compress(b"x" * 200000), headers={"Content-Encoding": "gzip"}))
    with pytest.raises(ConnectorError) as error:
        SafeFetcher(resolver=public_dns, connection_factory=connections, max_body_bytes=1000)(
            "https://employer.example/jobs", allowed_hosts=frozenset({"employer.example"}))
    assert error.value.code == "response_too_large"


def test_normal_gzip_and_wire_limit():
    connections = Connections(WireResponse(gzip.compress(b'{"jobs":[]}'), headers={"Content-Encoding": "gzip"}))
    assert SafeFetcher(resolver=public_dns, connection_factory=connections)("https://employer.example/jobs",
        allowed_hosts=frozenset({"employer.example"})).body == b'{"jobs":[]}'
    connections = Connections(WireResponse(b"x" * 100, headers={"Content-Length": "100"}))
    with pytest.raises(ConnectorError):
        SafeFetcher(resolver=public_dns, connection_factory=connections, max_wire_bytes=50)(
            "https://employer.example/jobs", allowed_hosts=frozenset({"employer.example"}))


def test_retry_after_seconds_and_http_date():
    assert retry_after("120") == 120
    assert retry_after("Tue, 08 Sep 2026 12:01:00 GMT", now=datetime(2026, 9, 8, 12, tzinfo=UTC)) == 60
    assert retry_after("bad") is None


def test_client_rate_limit_reports_delay_and_outage_is_not_closed():
    connections = Connections(WireResponse(status=429, headers={"Retry-After": "30"}))
    with pytest.raises(ConnectorError) as error:
        SafeFetcher(resolver=public_dns, connection_factory=connections)("https://employer.example/jobs",
            allowed_hosts=frozenset({"employer.example"}))
    assert error.value.retry_after_seconds == 30 and error.value.status == 429


def test_client_rejects_cookie_authority_injected_through_headers():
    with pytest.raises(ConnectorError) as error:
        SafeFetcher()("https://employer.example/jobs", allowed_hosts=frozenset({"employer.example"}),
                      headers={"Cookie": "fixture-secret"})
    assert error.value.code == "unsafe_header"
