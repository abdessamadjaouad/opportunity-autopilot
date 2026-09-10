"""Owner-initiated Google OAuth with encrypted, revocable offline credentials.

Only fixed Google HTTPS endpoints are used. No OAuth request is made by import,
configuration inspection, or constructing an authorization URL.
"""
import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.models import AuditEvent, AuthSession, Connection, Notification, now_iso
from app.security.auth import ensure_key

SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.send",
                    "https://www.googleapis.com/auth/gmail.readonly"})
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(ValueError):
    """Safe public reason; never includes Google payloads, tokens or request text."""
    def __init__(self, code, *, status=None):
        self.code, self.status = code, status
        super().__init__(code.replace("_", " "))


def _cipher(settings):
    return Fernet(ensure_key(settings).encode())


def _encrypt(settings, data):
    return _cipher(settings).encrypt(json.dumps(data, sort_keys=True).encode()).decode()


def _decrypt(settings, secret):
    try:
        value = json.loads(_cipher(settings).decrypt(secret.encode()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise GmailError("gmail_credentials_unreadable") from None


def _client_credentials(settings):
    path = settings.google_client_secret_file
    if not path or not Path(path).is_file() or not settings.google_client_id:
        raise GmailError("gmail_unconfigured_use_secure_server_configuration")
    try:
        path = Path(path)
        if path.stat().st_size > 20000:
            raise ValueError()
        if settings.production and path.stat().st_mode & 0o077:
            raise GmailError("gmail_client_secret_permissions_require_0600")
        text = path.read_text().strip()
        if text.startswith("{"):
            config = json.loads(text)
            config = config.get("web", config)
            if config.get("client_id") and config["client_id"] != settings.google_client_id:
                raise GmailError("gmail_client_id_mismatch")
            secret = config["client_secret"]
        else:
            secret = text
        if not isinstance(secret, str) or not secret or len(secret) > 10000:
            raise ValueError()
    except GmailError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise GmailError("gmail_client_secret_configuration_invalid") from None
    return settings.google_client_id, secret


def _redirect_uri(settings):
    parsed = urlsplit(settings.public_url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.scheme == "http" and (settings.production or parsed.hostname not in {"localhost", "127.0.0.1"})):
        raise GmailError("gmail_oauth_requires_https_or_loopback_origin")
    return settings.public_url.rstrip("/") + "/api/v1/connections/gmail/callback"


def _request(method, url, *, transport=None, **kwargs):
    """Bound JSON responses; credentials cannot follow redirects or environment proxies."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"oauth2.googleapis.com", "gmail.googleapis.com"}:
        raise GmailError("gmail_endpoint_not_allowed")
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream(method, url, **kwargs) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise GmailError("gmail_response_too_large", status=response.status_code)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    body = {}
                if not isinstance(body, dict):
                    raise GmailError("gmail_invalid_response", status=response.status_code)
                return response.status_code, body
    except httpx.HTTPError:
        raise GmailError("gmail_network_unavailable") from None


def _session_valid(session):
    if not session or session.data.get("kind") == "gmail_oauth":
        return False
    try:
        stamp = datetime.fromisoformat(session.expires_at)
        return stamp.tzinfo is not None and stamp > datetime.now(UTC)
    except (ValueError, TypeError):
        return False


def begin_oauth(db, settings, owner_session_id):
    client_id, _ = _client_credentials(settings)
    redirect = _redirect_uri(settings)
    state, verifier = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    identity = "oauth:" + hashlib.sha256(state.encode()).hexdigest()[:58]
    with db.write() as session:
        if not _session_valid(session.get(AuthSession, owner_session_id)):
            raise GmailError("owner_session_expired")
        # Supersede older incomplete flows for this owner session.
        for pending in session.scalars(select(AuthSession)):
            if pending.data.get("kind") == "gmail_oauth" and pending.data.get("owner_session_id") == owner_session_id:
                session.delete(pending)
        connection = session.get(Connection, "gmail")
        if not connection:
            connection = Connection(id="gmail", status="unconfigured", data={})
            session.add(connection)
        connection.data = {**connection.data, "oauth_pending_id": identity}
        session.add(AuthSession(id=identity,
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(), data={"kind": "gmail_oauth",
                "owner_session_id": owner_session_id, "encrypted_verifier": _encrypt(settings, {"verifier": verifier}),
                "redirect_uri": redirect}))
    return {"authorization_url": AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect,
        "response_type": "code", "scope": " ".join(sorted(SCOPES)), "access_type": "offline",
        "prompt": "consent select_account", "include_granted_scopes": "false", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"}),
        "requested_scopes": sorted(SCOPES), "read_access_notice":
        "Gmail read access covers your mailbox. The processing query is an application filter, not an OAuth permission boundary."}


def _token_payload(body, *, previous=None):
    previous = previous or {}
    scope = body.get("scope", previous.get("scope", ""))
    if not isinstance(scope, str) or set(scope.split()) != SCOPES:
        raise GmailError("gmail_granted_scopes_mismatch")
    access, refresh = body.get("access_token"), body.get("refresh_token", previous.get("refresh_token"))
    duration = body.get("expires_in")
    token_type = body.get("token_type", "Bearer")
    if (not isinstance(access, str) or not access or len(access) > 20000
            or not isinstance(refresh, str) or not refresh or len(refresh) > 20000
            or type(duration) is not int or not 1 <= duration <= 86400
            or not isinstance(token_type, str) or token_type.lower() != "bearer"):
        raise GmailError("gmail_offline_credentials_incomplete")
    value = {"access_token": access, "refresh_token": refresh, "scope": scope,
             "expires_at": (datetime.now(UTC) + timedelta(seconds=duration)).isoformat()}
    if type(body.get("refresh_token_expires_in")) is int and body["refresh_token_expires_in"] > 0:
        value["refresh_expires_at"] = (datetime.now(UTC) + timedelta(seconds=body["refresh_token_expires_in"])).isoformat()
    elif previous.get("refresh_expires_at"):
        value["refresh_expires_at"] = previous["refresh_expires_at"]
    return value


def mark_connection(db, status, code):
    with db.write() as session:
        connection = session.get(Connection, "gmail")
        if connection:
            connection.status = status
            connection.data = {**connection.data, "error": code, "last_error_at": now_iso()}
        key = "connection:gmail:" + code
        if not session.scalar(select(Notification).where(Notification.key == key)):
            session.add(Notification(key=key, data={"title": "Gmail needs attention", "reason": code,
                "urgency": "urgent", "message": "Review the Gmail connection in Sources and automation."}))


def complete_oauth(db, settings, state, code="", error="", *, transport=None):
    if not isinstance(state, str) or not 20 <= len(state) <= 200:
        raise GmailError("gmail_oauth_state_invalid_or_expired")
    identity = "oauth:" + hashlib.sha256(state.encode()).hexdigest()[:58]
    with db.write() as session:
        pending = session.get(AuthSession, identity)
        if not pending or pending.data.get("kind") != "gmail_oauth" or pending.expires_at <= now_iso():
            raise GmailError("gmail_oauth_state_invalid_or_expired")
        data = dict(pending.data)
        connection = session.get(Connection, "gmail")
        if not connection or connection.data.get("oauth_pending_id") != identity:
            raise GmailError("gmail_oauth_cancelled_or_superseded")
        if not _session_valid(session.get(AuthSession, data.get("owner_session_id"))):
            raise GmailError("owner_session_expired")
        session.delete(pending)
    # Consumed before token exchange. A timeout cannot reuse the authorization code.
    if error:
        with db.write() as session:
            connection = session.get(Connection, "gmail")
            status = connection.status if connection else "unconfigured"
            if connection and connection.data.get("oauth_pending_id") == identity:
                connection.data = {key: value for key, value in connection.data.items() if key != "oauth_pending_id"}
        return {"status": status, "reason": "gmail_authorization_declined", "return_url": "/automation"}
    if not isinstance(code, str) or not code or len(code) > 10000:
        raise GmailError("gmail_authorization_code_missing")
    client_id, secret = _client_credentials(settings)
    verifier = _decrypt(settings, data["encrypted_verifier"])["verifier"]
    status, body = _request("POST", TOKEN_URL, transport=transport, data={"grant_type": "authorization_code",
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": data["redirect_uri"], "code_verifier": verifier})
    if status != 200:
        raise GmailError("gmail_oauth_exchange_failed", status=status)
    tokens = _token_payload(body)
    status, profile = _request("GET", GMAIL_URL + "/profile", transport=transport,
        headers={"Authorization": "Bearer " + tokens["access_token"]})
    mailbox = profile.get("emailAddress")
    if status != 200 or not isinstance(mailbox, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", mailbox):
        raise GmailError("gmail_mailbox_verification_failed", status=status)
    with db.write() as session:
        if not _session_valid(session.get(AuthSession, data["owner_session_id"])):
            raise GmailError("owner_session_expired")
        connection = session.get(Connection, "gmail")
        if not connection or connection.data.get("oauth_pending_id") != identity:
            raise GmailError("gmail_oauth_cancelled_or_superseded")
        connection.status = "connected"
        connection.encrypted_secret = _encrypt(settings, tokens)
        connection.data = {"email_address": mailbox, "email": mailbox, "scopes": sorted(SCOPES),
            "connected_at": now_iso(), "access_expires_at": tokens["expires_at"],
            "refresh_expires_at": tokens.get("refresh_expires_at"), "error": None,
            "query_is_permission_boundary": False}
        session.add(AuditEvent(entity_id="owner", kind="gmail_connected", data={"scopes": sorted(SCOPES)}))
    return {"status": "connected", "email_address": mailbox, "return_url": "/automation"}


def access_credentials(db, settings, *, transport=None):
    with db.read() as session:
        connection = session.get(Connection, "gmail")
        if not connection or not connection.encrypted_secret or connection.status in {"unconfigured", "revoked", "expired"}:
            raise GmailError("gmail_connection_unconfigured_or_expired")
        original_secret = connection.encrypted_secret
        metadata = dict(connection.data)
    tokens = _decrypt(settings, original_secret)
    try:
        expires = datetime.fromisoformat(tokens["expires_at"])
        refresh_expires = datetime.fromisoformat(tokens["refresh_expires_at"]) if tokens.get("refresh_expires_at") else None
        if expires.tzinfo is None or refresh_expires and refresh_expires.tzinfo is None:
            raise ValueError()
        if refresh_expires and refresh_expires <= datetime.now(UTC):
            mark_connection(db, "expired", "gmail_refresh_token_expired")
            raise GmailError("gmail_refresh_token_expired")
        if set(tokens.get("scope", "").split()) != SCOPES:
            raise ValueError()
    except GmailError:
        raise
    except (KeyError, ValueError, TypeError):
        raise GmailError("gmail_credentials_require_reconnection") from None
    if expires > datetime.now(UTC) + timedelta(seconds=60):
        return tokens["access_token"], metadata
    client_id, secret = _client_credentials(settings)
    status, body = _request("POST", TOKEN_URL, transport=transport, data={"grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"], "client_id": client_id, "client_secret": secret})
    if status != 200:
        revoked = status in {400, 401} and body.get("error") in {"invalid_grant", "invalid_client"}
        mark_connection(db, "expired" if revoked else "error", "gmail_refresh_revoked" if revoked else "gmail_refresh_failed")
        raise GmailError("gmail_refresh_revoked" if revoked else "gmail_refresh_failed", status=status)
    refreshed = _token_payload(body, previous=tokens)
    with db.write() as session:
        connection = session.get(Connection, "gmail")
        # A disconnect/reconnect while the request was in flight wins.
        if not connection or connection.encrypted_secret != original_secret:
            raise GmailError("gmail_connection_changed_during_refresh")
        connection.encrypted_secret = _encrypt(settings, refreshed)
        connection.status = "connected"
        connection.data = {**connection.data, "access_expires_at": refreshed["expires_at"],
                           "refresh_expires_at": refreshed.get("refresh_expires_at"), "error": None}
    return refreshed["access_token"], metadata


def get_gmail_provider(db, settings, *, transport=None):
    from app.integrations.email_provider import get_gmail_provider as factory
    return factory(db, settings, transport=transport)
