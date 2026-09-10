import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4000 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid public URL")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise ValueError("Use an HTTPS public URL without embedded credentials")
    if parsed.port not in (None, 443) or host == "localhost" or host.endswith((".local", ".internal", ".localhost", ".localdomain")):
        raise ValueError("Private hosts and nonstandard ports are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address:
        if not address.is_global:
            raise ValueError("Private addresses are not allowed")
        authority = f"[{host}]" if address.version == 6 else host
    else:
        if "." not in host or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host):
            raise ValueError("Use a fully qualified public host name")
        try:
            authority = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("Invalid hostname") from None
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in authority.split(".")):
            raise ValueError("Invalid hostname")
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
    # Repeated query-key order can select a different requisition; retain it.
    return urlunsplit(("https", authority, parsed.path.rstrip("/") or "/", urlencode(query), ""))
