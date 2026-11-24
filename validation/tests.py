"""The traceability matrix must not be allowed to rot.

Every traceability matrix ever written in a word processor is wrong within a month:
someone renames a test, someone deletes one, someone adds a requirement and forgets the
row — and the document keeps asserting a link that no longer exists. Neither the
assessor nor the team can tell.

This test is the whole reason the matrix is code. If a requirement names a test that
does not exist, the build breaks. There is no state in which the matrix is quietly wrong.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner

from validation.requirements import ALL, PQ
from validation.runner import resolve


class TheMatrixCannotRotTests(SimpleTestCase):
    def test_every_requirement_names_a_test_that_actually_exists(self):
        """The load-bearing test in this file.

        A requirement whose verification has been renamed or deleted is worse than one
        with no verification at all: the first lies, the second merely admits a gap.
        """
        broken = []
        for req in ALL:
            if req.manual:
                continue
            for label in req.verified_by:
                _ids, err = resolve(label)
                if err:
                    broken.append(f"{req.id} -> {label} ({err})")

        self.assertEqual(
            broken, [],
            "The traceability matrix asserts a verification that does not exist:\n  "
            + "\n  ".join(broken))

    def test_the_rot_detector_actually_detects_rot(self):
        """The trap that this whole module nearly fell into, and did.

        ``build_suite(["does.not.Exist"])`` does NOT raise. unittest synthesises a
        ``_FailedTest`` placeholder that counts as one test, so the obvious check —
        ``countTestCases() == 0`` — never fires. The first version of the rot detector
        used exactly that check, and passed cleanly over FIVE dead links.

        A rot detector that cannot detect rot is worse than none: it certifies the lie.
        """
        # The naive check anyone would write, demonstrated to be useless:
        naive = DiscoverRunner(verbosity=0, interactive=False)
        self.assertEqual(naive.build_suite(["totally.made.up.Nonsense"]).countTestCases(), 1)

        # …and the real one:
        ids, err = resolve("totally.made.up.Nonsense")
        self.assertEqual(ids, [])
        self.assertTrue(err)

        # …which must not cry wolf on a name that does exist.
        ids, err = resolve("core.tests.AuditLogIsAppendOnlyTests")
        self.assertEqual(err, "")
        self.assertGreater(len(ids), 0)

    def test_every_automated_requirement_names_at_least_one_test(self):
        for req in ALL:
            if req.manual:
                continue
            self.assertTrue(
                req.verified_by,
                f"{req.id} claims to be automated but names no verifying test.")

    def test_a_manual_requirement_states_the_procedure(self):
        """"Operator to verify" with no procedure is not a control, it is a shrug."""
        for req in ALL:
            if req.manual:
                self.assertGreater(
                    len(req.manual), 40,
                    f"{req.id} is manual but gives no usable procedure.")
                self.assertFalse(
                    req.verified_by,
                    f"{req.id} is manual and must not claim an automated verification.")


class TheMatrixIsHonestTests(SimpleTestCase):
    def test_requirement_ids_are_unique(self):
        ids = [r.id for r in ALL]
        self.assertEqual(len(ids), len(set(ids)), "duplicate requirement id")

    def test_every_requirement_has_a_rationale(self):
        """A requirement with no rationale is a requirement nobody can argue with — and
        one nobody will maintain, because nobody remembers why it is there."""
        for r in ALL:
            self.assertGreater(len(r.rationale), 40, f"{r.id} has no real rationale.")

    def test_risk_grades_are_valid_and_not_all_high(self):
        """Grading everything HIGH is the same as grading nothing."""
        grades = {r.risk for r in ALL}
        self.assertTrue(grades <= {"high", "medium", "low"})

    def test_the_infrastructure_gaps_are_declared_manual_not_quietly_passed(self):
        """TLS, the read-only grant, DB-level audit protection and backups cannot be
        proven by a unit test. If any of them ever appears as 'automated', someone has
        written a test that proves nothing."""
        manual_ids = {r.id for r in ALL if r.manual}
        for expected in ("URS-060", "URS-061", "URS-062", "URS-063", "URS-064"):
            self.assertIn(expected, manual_ids)

    def test_no_PQ_threshold_is_silently_decided(self):
        """The acceptance thresholds are commitments the laboratory will be held to in
        an audit. They are proposed with reasoning; they are not ours to set."""
        for p in PQ:
            self.assertFalse(
                p.signed_off,
                f"{p.id} is marked signed off, but no laboratory has agreed it.")
            if p.needs_number:
                self.assertIn("[THRESHOLD]", p.statement,
                              f"{p.id} needs a number and must not pretend to have one.")
            else:
                # A qualitative criterion must not smuggle in a fake number.
                self.assertNotIn("[THRESHOLD]", p.statement)
            self.assertGreater(len(p.proposed), 30, f"{p.id} proposes nothing usable.")
