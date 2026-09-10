from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True)
class EmailEnvelope:
    message_id: str
    recipient: str
    subject: str
    text: str
    attachments: tuple[EmailAttachment, ...] = ()


@dataclass(frozen=True)
class ProviderAcceptance:
    provider_id: str
    thread_id: str = ""
    evidence: dict | None = None


class DefinitiveRejection(Exception):
    """Provider evidence proves that this request was NOT accepted."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EmailProvider(Protocol):
    is_simulator: bool

    def send(self, envelope: EmailEnvelope) -> ProviderAcceptance: ...

    def find_sent(self, message_id: str) -> list[dict]: ...

    def poll_messages(self, cursor: str | None = None) -> dict: ...
