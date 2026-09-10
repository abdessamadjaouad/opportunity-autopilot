"""Pure, fail-closed policy evaluation; this module cannot dispatch anything.

READY describes the supplied snapshot only. The submission service must reload
the policy, controls and evidence and atomically reserve an attempt before I/O.
"""

from datetime import UTC, datetime, timedelta

from app.core.models import (
    Controls, Decision, Disposition, Facts, Gate, History, Listing, Mode, Policy,
)


MAX_VERIFICATION_AGE = timedelta(minutes=30)


def _instant(value: object) -> datetime | None:
    """Reject malformed, naive and unusable dates without coercing input text."""
    if type(value) is not datetime:
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _token(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip()


def _allowlist(value: object) -> bool:
    # A string would implement substring matching; a mutable set could change
    # underneath a frozen policy. Neither is an approved immutable scope.
    return type(value) is frozenset and all(_token(item) for item in value)


def evaluate_readiness(
    facts: Facts, policy: Policy, controls: Controls, *, now: datetime,
) -> Decision:
    """Evaluate conservative gates without side effects or inferred authority."""
    now_utc = _instant(now)
    if now_utc is None:
        raise ValueError("now must be a valid timezone-aware datetime")
    if not isinstance(facts, Facts) or not isinstance(policy, Policy) or not isinstance(controls, Controls):
        raise TypeError("facts, policy and controls must use the readiness contracts")

    # Historical evidence takes precedence over availability so closing a
    # listing never conceals an uncertain or already-accepted attempt.
    if facts.history is History.SENT or facts.history is History.CONFIRMED:
        return Decision(Disposition.SKIP, ("already_applied",))
    if facts.history is not History.NONE:
        return Decision(Disposition.NEEDS_HUMAN, ("reconcile_submission",))
    if facts.listing is Listing.CLOSED:
        return Decision(Disposition.SKIP, ("listing_closed",))
    deadline = _instant(facts.deadline)
    if deadline is not None and deadline <= now_utc:
        return Decision(Disposition.SKIP, ("deadline_passed",))
    if facts.eligibility is Gate.FAIL:
        return Decision(Disposition.SKIP, ("mandatory_requirement_failed",))
    if facts.relocation is Gate.FAIL:
        return Decision(Disposition.SKIP, ("no_relocation_match",))

    holds = []
    if policy.approved is not True or policy.mode is not Mode.AUTOPILOT or not _token(policy.revision):
        holds.append("autopilot_not_authorized")
    if controls.paused is not False:
        holds.append("paused")
    if type(controls.remaining_daily_slots) is not int or controls.remaining_daily_slots <= 0:
        holds.append("daily_limit")
    if controls.budget_available is not True:
        holds.append("budget_limit")
    if controls.connection_healthy is not True:
        holds.append("connection_unavailable")
    if (type(policy.max_verification_age) is not timedelta
            or not timedelta(0) < policy.max_verification_age <= MAX_VERIFICATION_AGE):
        holds.append("invalid_freshness_policy")
    if not all(_allowlist(value) for value in (
        policy.allowed_countries, policy.allowed_tracks,
        policy.allowed_routes, policy.allowed_domains,
    )):
        holds.append("invalid_policy_allowlist")
    if holds:
        return Decision(Disposition.HOLD, tuple(holds))

    reasons = []
    if not _token(facts.opportunity_id):
        reasons.append("opportunity_identity_invalid")
    if facts.listing is not Listing.OPEN:
        reasons.append("listing_not_verified_open")
    verified_at = _instant(facts.verified_at)
    if verified_at is None:
        reasons.append("verification_missing_or_naive")
    elif verified_at > now_utc or now_utc - verified_at > policy.max_verification_age:
        reasons.append("verification_stale_or_future")
    if facts.deadline is not None and deadline is None:
        reasons.append("deadline_timezone_unknown")
    if facts.eligibility is not Gate.PASS:
        reasons.append("eligibility_unknown")
    if facts.relocation is not Gate.PASS:
        reasons.append("relocation_unknown")
    if facts.route_permission is not Gate.PASS:
        reasons.append("route_not_permitted")
    for value, allowed, reason in (
        (facts.country, policy.allowed_countries, "country_outside_policy"),
        (facts.track, policy.allowed_tracks, "track_outside_policy"),
        (facts.route, policy.allowed_routes, "route_outside_policy"),
        (facts.destination_domain, policy.allowed_domains, "destination_outside_policy"),
    ):
        if not _token(value) or value not in allowed:
            reasons.append(reason)
    if facts.route == "manual":
        reasons.append("manual_route")
    if facts.strong_fit is not True:
        reasons.append("fit_requires_review")
    if facts.profile_confirmed is not True:
        reasons.append("profile_unconfirmed")
    if (not _token(facts.profile_revision) or not _token(facts.packet_profile_revision)
            or facts.packet_profile_revision != facts.profile_revision):
        reasons.append("packet_profile_stale")
    if (not _token(facts.posting_hash) or not _token(facts.packet_posting_hash)
            or facts.packet_posting_hash != facts.posting_hash):
        reasons.append("packet_posting_stale")
    if not _token(facts.packet_policy_revision) or facts.packet_policy_revision != policy.revision:
        reasons.append("packet_policy_stale")
    if facts.documents_validated is not True:
        reasons.append("documents_unvalidated")
    if facts.answers_confirmed is not True:
        reasons.append("answers_unconfirmed")
    if type(facts.blockers) is not tuple or not all(_token(value) for value in facts.blockers):
        reasons.append("invalid_blockers")
    else:
        reasons.extend(f"blocker:{value}" for value in facts.blockers)
    if reasons:
        return Decision(Disposition.NEEDS_HUMAN, tuple(reasons))
    return Decision(Disposition.READY, ())
