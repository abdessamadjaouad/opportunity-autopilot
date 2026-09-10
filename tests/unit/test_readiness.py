from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from unittest import TestCase

from app.core.models import (
    Controls, Disposition, Facts, Gate, History, Listing, Mode, Policy,
)
from app.core.readiness import evaluate_readiness


class InvalidTimezone(tzinfo):
    def utcoffset(self, dt):
        raise ValueError("invalid offset")


class ReadinessTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        self.facts = Facts(
            opportunity_id="fixture-1", listing=Listing.OPEN,
            verified_at=self.now, deadline=self.now + timedelta(days=1),
            eligibility=Gate.PASS, relocation=Gate.PASS,
            route_permission=Gate.PASS, country="FR", track="data_ai",
            route="email", destination_domain="employer.example",
            strong_fit=True, profile_confirmed=True,
            profile_revision="profile-1", posting_hash="posting-1",
            packet_profile_revision="profile-1", packet_posting_hash="posting-1",
            packet_policy_revision="policy-1", documents_validated=True,
            answers_confirmed=True,
        )
        self.policy = Policy(
            revision="policy-1", approved=True, mode=Mode.AUTOPILOT,
            allowed_countries=frozenset({"FR"}),
            allowed_tracks=frozenset({"data_ai"}),
            allowed_routes=frozenset({"email"}),
            allowed_domains=frozenset({"employer.example"}),
        )
        self.controls = Controls(
            remaining_daily_slots=2, budget_available=True,
            connection_healthy=True,
        )

    def evaluate(self, facts=None, policy=None, controls=None):
        return evaluate_readiness(
            facts if facts is not None else self.facts,
            policy if policy is not None else self.policy,
            controls if controls is not None else self.controls,
            now=self.now,
        )

    def test_only_complete_approved_facts_are_ready(self):
        result = self.evaluate()
        self.assertIs(result.disposition, Disposition.READY)
        self.assertEqual(result.reasons, ())

    def test_closed_expired_ineligible_and_duplicate_are_skipped(self):
        cases = (
            {"listing": Listing.CLOSED},
            {"deadline": self.now},
            {"eligibility": Gate.FAIL},
            {"relocation": Gate.FAIL},
            {"history": History.SENT},
            {"history": History.CONFIRMED},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIs(self.evaluate(replace(self.facts, **changes)).disposition,
                              Disposition.SKIP)

    def test_uncertain_submission_requires_reconciliation(self):
        result = self.evaluate(replace(self.facts, history=History.UNCERTAIN))
        self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
        self.assertEqual(result.reasons, ("reconcile_submission",))

    def test_missing_stale_or_out_of_scope_facts_never_pass(self):
        cases = (
            {"listing": Listing.UNKNOWN},
            {"verified_at": None},
            {"verified_at": self.now - timedelta(minutes=31)},
            {"verified_at": self.now + timedelta(seconds=1)},
            {"verified_at": self.now.replace(tzinfo=None)},
            {"deadline": self.now.replace(tzinfo=None)},
            {"eligibility": Gate.UNKNOWN},
            {"relocation": Gate.UNKNOWN},
            {"route_permission": Gate.UNKNOWN},
            {"route_permission": Gate.FAIL},
            {"country": "MA"},
            {"track": "senior_architect"},
            {"route": "manual"},
            {"destination_domain": "employer.example.attacker.invalid"},
            {"strong_fit": False},
            {"profile_confirmed": False},
            {"packet_profile_revision": "profile-old"},
            {"packet_posting_hash": "posting-old"},
            {"packet_policy_revision": "policy-old"},
            {"documents_validated": False},
            {"answers_confirmed": False},
            {"blockers": ("camera_consent",)},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.evaluate(replace(self.facts, **changes))
                self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
                self.assertTrue(result.reasons)

    def test_disabled_or_unapproved_policy_holds_submission(self):
        for policy in (Policy(), replace(self.policy, approved=False),
                       replace(self.policy, mode=Mode.DRAFT_ONLY),
                       replace(self.policy, revision="")):
            with self.subTest(policy=policy):
                self.assertIs(self.evaluate(policy=policy).disposition, Disposition.HOLD)

    def test_runtime_controls_hold_submission(self):
        for changes in ({"paused": True}, {"remaining_daily_slots": 0},
                        {"budget_available": False}, {"connection_healthy": False}):
            with self.subTest(changes=changes):
                result = self.evaluate(controls=replace(self.controls, **changes))
                self.assertIs(result.disposition, Disposition.HOLD)

    def test_string_booleans_cannot_grant_authority(self):
        result = self.evaluate(policy=replace(self.policy, approved="true"))
        self.assertIs(result.disposition, Disposition.HOLD)
        result = self.evaluate(replace(self.facts, profile_confirmed="true"))
        self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)

    def test_unknown_history_is_not_treated_as_never_applied(self):
        for history in ("invalid", "none", None, 0, [], {}):
            with self.subTest(history=history):
                result = self.evaluate(replace(self.facts, history=history))
                self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)

    def test_clock_must_be_timezone_aware(self):
        for now in (self.now.replace(tzinfo=None), None, "2026-09-08", date(2026, 9, 8),
                    self.now.replace(tzinfo=InvalidTimezone())):
            with self.subTest(now_type=type(now)):
                with self.assertRaises(ValueError):
                    evaluate_readiness(self.facts, self.policy, self.controls, now=now)

    def test_malformed_datetimes_need_review_without_crashing(self):
        for field in ("verified_at", "deadline"):
            for value in ("2026-09-08T12:00:00Z", 1, [], {}, date(2026, 9, 8),
                          self.now.replace(tzinfo=InvalidTimezone())):
                with self.subTest(field=field, value_type=type(value)):
                    result = self.evaluate(replace(self.facts, **{field: value}))
                    self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)

    def test_invalid_freshness_duration_holds_without_crashing(self):
        for duration in (None, 1800, "30 minutes", True, timedelta(0),
                         timedelta(seconds=-1), timedelta(minutes=31)):
            with self.subTest(duration=duration):
                result = self.evaluate(policy=replace(self.policy, max_verification_age=duration))
                self.assertIs(result.disposition, Disposition.HOLD)
                self.assertIn("invalid_freshness_policy", result.reasons)

    def test_exact_freshness_boundary_and_stricter_policy(self):
        facts = replace(self.facts, verified_at=self.now - timedelta(minutes=30))
        self.assertIs(self.evaluate(facts).disposition, Disposition.READY)
        facts = replace(facts, verified_at=facts.verified_at - timedelta(microseconds=1))
        self.assertIs(self.evaluate(facts).disposition, Disposition.NEEDS_HUMAN)
        policy = replace(self.policy, max_verification_age=timedelta(minutes=5))
        facts = replace(self.facts, verified_at=self.now - timedelta(minutes=6))
        self.assertIs(self.evaluate(facts, policy).disposition, Disposition.NEEDS_HUMAN)

    def test_datetime_offsets_compare_the_same_instants(self):
        facts = replace(self.facts, verified_at=self.now.astimezone(timezone(timedelta(hours=1))))
        self.assertIs(self.evaluate(facts).disposition, Disposition.READY)

    def test_boolean_lookalikes_never_grant_authority(self):
        for field in ("strong_fit", "profile_confirmed", "documents_validated", "answers_confirmed"):
            for value in (1, "true", [True]):
                with self.subTest(field=field, value=value):
                    result = self.evaluate(replace(self.facts, **{field: value}))
                    self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
        for field, value in (("paused", 0), ("paused", "false"), ("budget_available", 1),
                             ("connection_healthy", "true"), ("remaining_daily_slots", True),
                             ("remaining_daily_slots", 1.0), ("remaining_daily_slots", "2")):
            with self.subTest(field=field, value=value):
                result = self.evaluate(controls=replace(self.controls, **{field: value}))
                self.assertIs(result.disposition, Disposition.HOLD)
        self.assertIs(self.evaluate(policy=replace(self.policy, approved=1)).disposition,
                      Disposition.HOLD)

    def test_enum_strings_do_not_grant_authority(self):
        for field, value in (("listing", "open"), ("eligibility", "pass"),
                             ("relocation", "pass"), ("route_permission", "pass")):
            with self.subTest(field=field):
                result = self.evaluate(replace(self.facts, **{field: value}))
                self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
        self.assertIs(self.evaluate(policy=replace(self.policy, mode="autopilot")).disposition,
                      Disposition.HOLD)

    def test_malformed_allowlists_cannot_act_as_substring_policies(self):
        for field in ("allowed_countries", "allowed_tracks", "allowed_routes", "allowed_domains"):
            allowed = getattr(self.policy, field)
            for value in (None, "FR,data_ai,email,employer.example", list(allowed), set(allowed),
                          frozenset({""}), frozenset({None})):
                with self.subTest(field=field, value=value):
                    result = self.evaluate(policy=replace(self.policy, **{field: value}))
                    self.assertIs(result.disposition, Disposition.HOLD)

    def test_non_string_ids_and_scope_values_do_not_authorize(self):
        for field in ("opportunity_id", "country", "track", "route", "destination_domain",
                      "profile_revision", "posting_hash", "packet_profile_revision",
                      "packet_posting_hash", "packet_policy_revision"):
            for value in (None, 1, [], " "):
                with self.subTest(field=field, value=value):
                    result = self.evaluate(replace(self.facts, **{field: value}))
                    self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
        for value in (1, [], " ", None):
            with self.subTest(revision=value):
                self.assertIs(self.evaluate(policy=replace(self.policy, revision=value)).disposition,
                              Disposition.HOLD)

    def test_malformed_blockers_are_not_silently_ignored(self):
        for blockers in (None, "", "captcha", [], {}, (None,), ("",)):
            with self.subTest(blockers=blockers):
                result = self.evaluate(replace(self.facts, blockers=blockers))
                self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)

    def test_uncertainty_preserves_reconciliation_even_when_listing_closed(self):
        facts = replace(self.facts, history=History.UNCERTAIN, listing=Listing.CLOSED)
        self.assertEqual(self.evaluate(facts).reasons, ("reconcile_submission",))

    def test_manual_route_needs_human_even_if_allowlisted(self):
        facts = replace(self.facts, route="manual")
        policy = replace(self.policy, allowed_routes=frozenset({"manual"}))
        result = self.evaluate(facts, policy)
        self.assertIs(result.disposition, Disposition.NEEDS_HUMAN)
        self.assertIn("manual_route", result.reasons)

    def test_known_missing_deadline_does_not_invent_a_cutoff(self):
        self.assertIs(self.evaluate(replace(self.facts, deadline=None)).disposition,
                      Disposition.READY)
