"""Immutable readiness contracts, independent of persistence and external I/O.

These dataclasses deliberately do not coerce values. Runtime input validation is
also performed by the gate, so a truthy string can never authorize a submission.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class Gate(Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Listing(Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class History(Enum):
    NONE = "none"
    SENT = "sent_unconfirmed"
    CONFIRMED = "submitted_confirmed"
    UNCERTAIN = "submission_uncertain"


class Mode(Enum):
    DRAFT_ONLY = "draft_only"
    AUTOPILOT = "autopilot"


class Disposition(Enum):
    SKIP = "skip"
    HOLD = "hold"
    NEEDS_HUMAN = "needs_human"
    READY = "ready"


@dataclass(frozen=True)
class Facts:
    opportunity_id: str
    listing: Listing = Listing.UNKNOWN
    verified_at: datetime | None = None
    deadline: datetime | None = None
    history: History = History.NONE
    eligibility: Gate = Gate.UNKNOWN
    relocation: Gate = Gate.UNKNOWN
    route_permission: Gate = Gate.UNKNOWN
    country: str = ""
    track: str = ""
    route: str = "manual"
    destination_domain: str = ""
    strong_fit: bool = False
    profile_confirmed: bool = False
    profile_revision: str = ""
    posting_hash: str = ""
    packet_profile_revision: str = ""
    packet_posting_hash: str = ""
    packet_policy_revision: str = ""
    documents_validated: bool = False
    answers_confirmed: bool = False
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    revision: str = ""
    approved: bool = False
    mode: Mode = Mode.DRAFT_ONLY
    allowed_countries: frozenset[str] = frozenset()
    allowed_tracks: frozenset[str] = frozenset()
    allowed_routes: frozenset[str] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    max_verification_age: timedelta = timedelta(minutes=30)


@dataclass(frozen=True)
class Controls:
    paused: bool = False
    remaining_daily_slots: int = 0
    budget_available: bool = False
    connection_healthy: bool = False


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    reasons: tuple[str, ...]
