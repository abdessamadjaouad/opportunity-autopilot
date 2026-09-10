"""Gmail transport: acceptance evidence only, with no automatic send retries."""
import base64
import binascii
import json
import re
from datetime import UTC, datetime, timedelta
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from pathlib import PurePath
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.applications.contracts import DefinitiveRejection, EmailEnvelope, ProviderAcceptance
from app.integrations.gmail import GMAIL_URL, GmailError, _request, access_credentials, mark_connection
from app.models import Connection


def _message_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"<[^\s<>@]{1,150}@[^\s<>@]{1,150}>", value):
        raise DefinitiveRejection("invalid_message_id")
    return value


def _mailbox(value):
    if not isinstance(value, str) or len(value) > 320 or any(char in value for char in "\r\n\x00"):
        raise DefinitiveRejection("invalid_email_address")
    try:
        parsed = Address(addr_spec=value)
        if parsed.addr_spec != value or not parsed.username or "." not in parsed.domain:
            raise ValueError()
    except (ValueError, IndexError):
        raise DefinitiveRejection("invalid_email_address") from None
    return value


def serialize_envelope(envelope, sender):
    if not isinstance(envelope, EmailEnvelope):
        raise DefinitiveRejection("invalid_email_envelope")
    if (not isinstance(envelope.subject, str) or not 1 <= len(envelope.subject) <= 998
            or any(char in envelope.subject for char in "\r\n\x00")):
        raise DefinitiveRejection("invalid_email_subject")
    if not isinstance(envelope.text, str) or not envelope.text or len(envelope.text) > 200000:
        raise DefinitiveRejection("invalid_email_body")
    if type(envelope.attachments) is not tuple or len(envelope.attachments) > 10:
        raise DefinitiveRejection("invalid_email_attachments")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = _mailbox(sender), _mailbox(envelope.recipient)
    message["Subject"], message["Message-ID"] = envelope.subject, _message_id(envelope.message_id)
    message["Date"] = format_datetime(datetime.now(UTC))
    message.set_content(envelope.text)
    total = 0
    for attachment in envelope.attachments:
        filename = attachment.filename
        if (not isinstance(filename, str) or not filename or len(filename) > 200 or PurePath(filename).name != filename
                or any(char in filename for char in "\\\r\n\x00")
                or attachment.mime_type not in {"application/pdf", "text/plain"}
                or type(attachment.content) is not bytes or not attachment.content or len(attachment.content) > 10_000_000):
            raise DefinitiveRejection("invalid_email_attachment")
        total += len(attachment.content)
        if total > 20_000_000:
            raise DefinitiveRejection("email_packet_too_large")
        major, minor = attachment.mime_type.split("/", 1)
        message.add_attachment(attachment.content, maintype=major, subtype=minor, filename=filename)
    raw = message.as_bytes()
    if len(raw) > 28_000_000:
        raise DefinitiveRejection("email_packet_too_large")
    return raw


def _decode_text(payload):
    plain, fallback, stack, visited = [], [], [payload], 0
    while stack:
        part = stack.pop()
        visited += 1
        if visited > 1000 or not isinstance(part, dict):
            raise GmailError("gmail_message_structure_invalid")
        if part.get("filename"):
            continue  # Never download, execute or parse attachments during polling.
        stack.extend(part.get("parts", []))
        mime = part.get("mimeType", "")
        value = (part.get("body") or {}).get("data")
        if mime not in {"text/plain", "text/html"} or not isinstance(value, str):
            continue
        if len(value) > 600000:
            raise GmailError("gmail_message_text_too_large")
        try:
            text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            raise GmailError("gmail_message_encoding_invalid") from None
        if mime == "text/html":
            soup = BeautifulSoup(text, "html.parser")
            for node in soup.find_all(["script", "style", "iframe", "object", "embed", "template"]):
                node.decompose()
            fallback.append(soup.get_text(" ", strip=True))
        else:
            plain.append(text)
    combined = "\n".join(plain or fallback)
    return combined[:200000], len(combined) > 200000


def normalize_message(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise GmailError("gmail_message_invalid")
    payload = raw.get("payload") or {}
    headers = {}
    first_authentication = None
    for header in payload.get("headers", []):
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            name, value = header["name"].lower(), header["value"][:10000]
            if name == "authentication-results" and first_authentication is None:
                first_authentication = value
            headers[name] = (headers.get(name, "") + " " + value).strip()
    text, truncated = _decode_text(payload)
    try:
        stamp = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, UTC).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        stamp = None
    sender_name, sender = parseaddr(headers.get("from", ""))
    authentication = first_authentication or ""
    authenticated_domain = re.search(r"\bdmarc=pass\b[^;]*\bheader\.from=([^\s;]+)", authentication, re.I)
    authenticated = bool(authentication.startswith("mx.google.com;") and authenticated_domain and
        authenticated_domain.group(1).strip('"').casefold() == sender.rsplit("@", 1)[-1].casefold())
    return {"external_id": raw["id"], "provider_id": raw["id"], "thread_id": raw.get("threadId", ""),
        "message_id": headers.get("message-id", ""), "in_reply_to": headers.get("in-reply-to", ""),
        "references": re.findall(r"<[^<>\s]+>", headers.get("references", "")),
        "sender": sender, "sender_name": sender_name, "recipient": parseaddr(headers.get("to", ""))[1],
        "subject": headers.get("subject", ""), "body": text, "body_truncated": truncated,
        "date": stamp, "date_header": headers.get("date", ""), "labels": raw.get("labelIds", []),
        "auto_submitted": headers.get("auto-submitted", ""), "list_id": headers.get("list-id", ""),
        "precedence": headers.get("precedence", ""), "authentication_results": headers.get("authentication-results", ""),
        "content_type": headers.get("content-type", payload.get("mimeType", "")),
        "is_fixture": False, "provider": "gmail", "authenticated_sender": authenticated}


class GmailProvider:
    is_simulator = False

    def __init__(self, db, settings, *, transport=None):
        self.db, self.settings, self.transport = db, settings, transport

    def _get(self, path, **params):
        from app.models import uid
        from app.workers.budget import reserve
        with self.db.write() as session:
            reserve(session, self.settings, uid(), "gmail_read")
        access, _ = access_credentials(self.db, self.settings, transport=self.transport)
        status, body = _request("GET", GMAIL_URL + path, transport=self.transport,
            headers={"Authorization": "Bearer " + access}, params=params)
        if status == 401:
            mark_connection(self.db, "expired", "gmail_access_revoked")
            raise GmailError("gmail_access_revoked", status=status)
        if status != 200:
            if status in {403, 429} or status >= 500:
                mark_connection(self.db, "error", "gmail_read_unavailable")
            raise GmailError("gmail_read_unavailable", status=status)
        return body

    def send(self, envelope):
        if self.settings.live_submissions_enabled is not True:
            raise DefinitiveRejection("live_submissions_disabled")
        try:
            access, metadata = access_credentials(self.db, self.settings, transport=self.transport)
        except GmailError as error:
            # No send request has begun, so this failure is definitely safe.
            raise DefinitiveRejection(error.code) from None
        raw = serialize_envelope(envelope, metadata.get("email_address", ""))
        if hasattr(self, "before_external_write") and self.before_external_write() is not True:
            raise DefinitiveRejection("final_effect_gate_refused")
        status, body = _request("POST", GMAIL_URL + "/messages/send", transport=self.transport,
            headers={"Authorization": "Bearer " + access}, json={"raw": base64.urlsafe_b64encode(raw).decode()})
        if status in {400, 401, 403, 404, 413, 429}:
            if status == 401:
                mark_connection(self.db, "expired", "gmail_access_revoked")
            raise DefinitiveRejection(f"gmail_send_rejected_{status}")
        if status != 200 or not isinstance(body.get("id"), str) or not body["id"]:
            # Timeouts, 5xx and malformed success responses are uncertain. The
            # dispatch service must reconcile Sent Mail, never blindly retry.
            raise GmailError("gmail_send_acceptance_uncertain", status=status)
        return ProviderAcceptance(body["id"], body.get("threadId", ""), {
            "kind": "gmail_provider_acceptance", "message_id": envelope.message_id,
            "recipient": envelope.recipient, "delivery_confirmed": False})

    def find_sent(self, message_id):
        _message_id(message_id)
        body = self._get("/messages", q=f"in:sent rfc822msgid:{message_id}", maxResults=100)
        if body.get("nextPageToken"):
            raise GmailError("gmail_sent_search_ambiguous")
        records = []
        references = body.get("messages", [])
        if not isinstance(references, list) or len(references) > 100 or any(not isinstance(item, dict) for item in references):
            raise GmailError("gmail_message_list_invalid")
        for reference in references:
            identity = _provider_id(reference.get("id"))
            raw = self._get("/messages/" + quote(identity, safe=""), format="full")
            record = normalize_message(raw)
            if record["message_id"] == message_id and "SENT" in record["labels"]:
                records.append(record)
        return records

    def poll_messages(self, cursor=None):
        now = int(datetime.now(UTC).timestamp())
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 10000:
                    raise ValueError()
                current = json.loads(cursor)
                if (not isinstance(current, dict) or current.get("v") != 1
                        or type(current.get("after")) is not int or current["after"] < 0
                        or current.get("until") is not None and type(current["until"]) is not int
                        or current.get("page") is not None and not isinstance(current["page"], str)):
                    raise ValueError()
            except (ValueError, TypeError):
                raise GmailError("gmail_poll_cursor_invalid") from None
        else:
            current = {"v": 1, "after": now - int(timedelta(days=30).total_seconds())}
        until = current.get("until") or now + 1
        if until < current["after"] or until > now + 300 or current["after"] > now + 300:
            raise GmailError("gmail_poll_cursor_invalid")
        query = self.settings.gmail_query
        if not isinstance(query, str) or len(query) > 5000 or any(char in query for char in "\r\n\x00"):
            raise GmailError("gmail_processing_query_invalid")
        params = {"q": f"({query}) after:{current['after']} before:{until} -in:sent", "maxResults": 100}
        if current.get("page"):
            params["pageToken"] = current["page"]
        try:
            body = self._get("/messages", **params)
        except GmailError as error:
            if error.status != 400 or "pageToken" not in params:
                raise
            # An expired page token restarts this same bounded time window.
            # Already imported external IDs deduplicate in persistent services.
            params.pop("pageToken")
            body = self._get("/messages", **params)
        references = body.get("messages", [])
        if not isinstance(references, list) or len(references) > 100 or any(not isinstance(item, dict) for item in references):
            raise GmailError("gmail_message_list_invalid")
        messages = []
        for reference in references:
            identity = _provider_id(reference.get("id"))
            raw = self._get("/messages/" + quote(identity, safe=""), format="full")
            if "SENT" not in raw.get("labelIds", []):
                messages.append(normalize_message(raw))
        if body.get("nextPageToken"):
            if not isinstance(body["nextPageToken"], str) or len(body["nextPageToken"]) > 8000:
                raise GmailError("gmail_poll_cursor_invalid")
            next_cursor = {"v": 1, "after": current["after"], "until": until, "page": body["nextPageToken"]}
        else:
            # A small overlap is intentional; caller deduplicates external IDs.
            next_cursor = {"v": 1, "after": max(0, until - 120), "until": None}
        return {"messages": messages, "cursor": json.dumps(next_cursor, sort_keys=True),
                "complete": not bool(body.get("nextPageToken")), "query_is_permission_boundary": False}


def _provider_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise GmailError("gmail_message_id_invalid")
    return value


def get_gmail_provider(db, settings, *, transport=None):
    with db.read() as session:
        connection = session.get(Connection, "gmail")
        if not connection or not connection.encrypted_secret or connection.status in {"unconfigured", "expired", "revoked"}:
            raise GmailError("gmail_connection_unconfigured_or_expired")
    return GmailProvider(db, settings, transport=transport)
