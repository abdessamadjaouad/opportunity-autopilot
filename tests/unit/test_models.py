from dataclasses import FrozenInstanceError
from unittest import TestCase

from app.core.models import Controls, Facts, Gate, Mode, Policy


class ModelTests(TestCase):
    def test_defaults_cannot_authorize_a_submission(self):
        policy = Policy()
        facts = Facts(opportunity_id="fixture-1")
        self.assertFalse(policy.approved)
        self.assertIs(policy.mode, Mode.DRAFT_ONLY)
        self.assertEqual(policy.allowed_domains, frozenset())
        self.assertIs(facts.eligibility, Gate.UNKNOWN)
        self.assertIs(facts.relocation, Gate.UNKNOWN)
        self.assertFalse(facts.documents_validated)
        self.assertEqual(Controls().remaining_daily_slots, 0)

    def test_policy_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            Policy().approved = True

    def test_facts_and_controls_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            Facts(opportunity_id="fixture-1").profile_confirmed = True
        with self.assertRaises(FrozenInstanceError):
            Controls().remaining_daily_slots = 100

    def test_policy_collections_and_blockers_have_immutable_defaults(self):
        for name in ("allowed_countries", "allowed_tracks", "allowed_routes", "allowed_domains"):
            with self.subTest(name=name):
                self.assertIsInstance(getattr(Policy(), name), frozenset)
        self.assertIsInstance(Facts(opportunity_id="fixture-1").blockers, tuple)
