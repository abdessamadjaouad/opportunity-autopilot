"""Small read-only HTTPS client with a pinned public destination per connection."""
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.security.urls import canonical_url


class ConnectorError(Exception):
    def __init__(self, code: str, message: str = "", *, retry_after_seconds: int | None = None,
                 status: int | None = None):
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.status = status
        super().__init__(message or code.replace("_", " "))


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""


def retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return min(604800, max(1, int(value.strip())))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return min(604800, max(1, int((stamp - (now or datetime.now(UTC))).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return None


def network_url(value: str) -> str:
    """Validate host without changing path/query semantics during HTTP requests."""
    try:
        validated, original = urlsplit(canonical_url(value)), urlsplit(value)
    except (ValueError, TypeError) as error:
        raise ConnectorError("unsafe_url", str(error)) from None
    return urlunsplit(("https", validated.netloc, original.path or "/", original.query, ""))


def public_addresses(host: str, resolver=socket.getaddrinfo) -> list[tuple]:
    try:
        records = resolver(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise ConnectorError("dns_unavailable") from None
    if not records or len(records) > 32:
        raise ConnectorError("dns_unavailable")
    result = []
    for family, kind, protocol, _, address in records:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise ConnectorError("unsafe_address") from None
        if (family not in {socket.AF_INET, socket.AF_INET6} or kind != socket.SOCK_STREAM
                or protocol != socket.IPPROTO_TCP or address[1] != 443
                or not ip.is_global or ip.is_multicast or ip.is_reserved
                or (getattr(ip, "ipv4_mapped", None) is not None and not ip.ipv4_mapped.is_global)):
            raise ConnectorError("unsafe_address", "Every resolved address must be public unicast")
        pair = (family, address)
        if pair not in result:
            result.append(pair)
    return result


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family, address = self.address
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        try:
            # No second DNS lookup: connect to the previously checked sockaddr.
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SafeFetcher:
    """GET only; no cookies, proxies, credentials, browser session or request body."""
    def __init__(self, *, resolver=socket.getaddrinfo, connection_factory=_PinnedHTTPSConnection,
                 timeout=15, max_wire_bytes=8_000_000, max_body_bytes=8_000_000, max_redirects=3):
        self.resolver = resolver
        self.connection_factory = connection_factory
        self.timeout = timeout
        self.max_wire_bytes = max_wire_bytes
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects

    def __call__(self, url: str, *, allowed_hosts: frozenset[str], headers: dict[str, str] | None = None):
        if not isinstance(allowed_hosts, frozenset) or not allowed_hosts:
            raise ConnectorError("host_not_permitted")
        current, seen = network_url(url), set()
        request_headers = {"Accept": "application/json, application/rss+xml, application/atom+xml, text/html;q=0.8",
                           "Accept-Encoding": "identity", "User-Agent": "OpportunityAutopilot/0.1 (public listing reader)"}
        for key, value in (headers or {}).items():
            if key.lower() not in {"if-none-match", "if-modified-since", "accept"}:
                raise ConnectorError("unsafe_header")
            if not isinstance(value, str) or len(value) > 2000 or any(ord(char) < 32 for char in value):
                raise ConnectorError("unsafe_header")
            request_headers[key] = value
        for _ in range(self.max_redirects + 1):
            parsed = urlsplit(current)
            if parsed.hostname not in allowed_hosts:
                raise ConnectorError("host_not_permitted")
            if current in seen:
                raise ConnectorError("redirect_loop")
            seen.add(current)
            addresses = public_addresses(parsed.hostname, self.resolver)
            response = None
            # Failed connect/read GETs are safe to retry on a later scheduled run;
            # avoid multiplying load by trying every DNS answer here.
            connection = self.connection_factory(parsed.hostname, addresses[0], self.timeout)
            try:
                started = time.monotonic()
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                connection.request("GET", path, headers=request_headers)
                response = connection.getresponse()
                received_headers = {key.lower(): value for key, value in response.getheaders()}
                if response.status in {301, 302, 303, 307, 308}:
                    location = received_headers.get("location")
                    if not location:
                        raise ConnectorError("invalid_redirect")
                    current = network_url(urljoin(current, location))
                    continue
                if response.status == 429:
                    raise ConnectorError("rate_limited", retry_after_seconds=retry_after(
                        received_headers.get("retry-after")), status=429)
                if response.status == 304:
                    return FetchResponse(304, b"", received_headers, current)
                raw_length = received_headers.get("content-length")
                if raw_length:
                    try:
                        length = int(raw_length)
                    except ValueError:
                        raise ConnectorError("invalid_response_length") from None
                    if length < 0 or length > self.max_wire_bytes:
                        raise ConnectorError("response_too_large")
                encoding = received_headers.get("content-encoding", "identity").lower()
                if encoding not in {"", "identity", "gzip"}:
                    raise ConnectorError("unsupported_encoding")
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
                result, wire = bytearray(), 0
                while True:
                    if time.monotonic() - started > self.timeout * 2:
                        raise ConnectorError("response_timeout")
                    chunk = response.read(16384)
                    if not chunk:
                        break
                    wire += len(chunk)
                    if wire > self.max_wire_bytes:
                        raise ConnectorError("response_too_large")
                    remaining = self.max_body_bytes - len(result)
                    result.extend(decoder.decompress(chunk, remaining + 1) if decoder else chunk)
                    if len(result) > self.max_body_bytes:
                        raise ConnectorError("response_too_large")
                    if decoder and (decoder.unconsumed_tail or decoder.unused_data):
                        raise ConnectorError("response_too_large", "Unsupported or oversized compressed response")
                if decoder and not decoder.eof:
                    raise ConnectorError("invalid_compressed_response")
                if response.status >= 500:
                    raise ConnectorError("source_unavailable", status=response.status)
                if response.status not in {200, 404, 410}:
                    raise ConnectorError("http_error", status=response.status)
                return FetchResponse(response.status, bytes(result), received_headers, current)
            except ConnectorError:
                raise
            except (OSError, http.client.HTTPException, zlib.error):
                raise ConnectorError("network_unavailable") from None
            finally:
                if response:
                    response.close()
                connection.close()
        raise ConnectorError("too_many_redirects")


safe_fetch = SafeFetcher()
