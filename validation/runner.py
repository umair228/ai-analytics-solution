"""Execute the traceability matrix and collect the evidence.

The load-bearing rule: **a requirement whose named test does not exist is a FAILURE,
not a silent pass.** That is the single mechanism that stops a traceability matrix
rotting — and every traceability matrix rots, because somebody always renames a test
and the Word document keeps asserting the link.
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field

import django
from django.utils import timezone

from .requirements import ALL, Requirement


@dataclass
class Outcome:
    requirement: Requirement
    status: str                     # passed | failed | missing | manual
    tests_run: int = 0
    failures: list = field(default_factory=list)
    missing: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("passed", "manual")


def _test_ids(suite):
    for item in suite:
        if hasattr(item, "__iter__"):
            yield from _test_ids(item)
        else:
            cls = item.__class__
            yield f"{cls.__module__}.{cls.__qualname__}.{item._testMethodName}"


def _matches(label: str, test_id: str) -> bool:
    """A label names either a module, a class, or a single method."""
    return test_id == label or test_id.startswith(label + ".")


def resolve(label: str) -> tuple[list[str], str]:
    """The real test ids a label names, or ``([], reason)`` if it names nothing.

    ⚠️ THE TRAP THIS FUNCTION EXISTS FOR — and which this module was itself written
    with, before it was caught:

    ``DiscoverRunner.build_suite(["does.not.Exist"])`` does **not** raise. unittest's
    loader synthesises a ``unittest.loader._FailedTest`` placeholder that *counts as
    one test* and only fails when it is run. So the obvious rot-check —
    ``countTestCases() == 0`` — NEVER fires, and a traceability matrix full of dead
    links passes its own integrity check with a smile.

    Which is precisely the failure the matrix exists to prevent. Detect the placeholder.
    """
    from django.test.runner import DiscoverRunner

    runner = DiscoverRunner(verbosity=0, interactive=False)
    try:
        suite = runner.build_suite([label])
    except Exception as exc:  # noqa: BLE001
        return [], str(exc).splitlines()[0]

    ids = list(_test_ids(suite))
    if not ids:
        return [], "matches no test"
    if any("_FailedTest" in i for i in ids):
        return [], "unittest could not load it — the name does not exist"
    return ids, ""


def run(verbosity=0) -> dict:
    """Run every automated test named by the matrix; return the evidence."""
    from django.test.runner import DiscoverRunner
    from django.test.utils import setup_test_environment, teardown_test_environment

    automated = [r for r in ALL if not r.manual]
    labels = sorted({lbl for r in automated for lbl in r.verified_by})

    setup_test_environment()
    runner = DiscoverRunner(verbosity=verbosity, interactive=False)
    old_config = runner.setup_databases()
    try:
        suite = runner.build_suite(labels)
        all_ids = list(_test_ids(suite))
        result = runner.run_suite(suite)
    finally:
        runner.teardown_databases(old_config)
        teardown_test_environment()

    failed_ids = set()
    for test, _tb in list(result.failures) + list(result.errors):
        cls = test.__class__
        failed_ids.add(f"{cls.__module__}.{cls.__qualname__}."
                       f"{getattr(test, '_testMethodName', '')}")

    outcomes = []
    for r in ALL:
        if r.manual:
            outcomes.append(Outcome(requirement=r, status="manual"))
            continue

        ran, failures, missing = [], [], []
        for label in r.verified_by:
            hits = [t for t in all_ids if _matches(label, t)]
            if not hits:
                # The test this requirement claims to be verified by does not exist.
                # A matrix that quietly tolerates this is a matrix that lies.
                missing.append(label)
                continue
            ran.extend(hits)
            failures.extend(t for t in hits if t in failed_ids)

        if missing:
            status = "missing"
        elif failures:
            status = "failed"
        else:
            status = "passed"
        outcomes.append(Outcome(requirement=r, status=status, tests_run=len(ran),
                                failures=sorted(set(failures)), missing=missing))

    return {
        "generated_at": timezone.now().isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "django": django.get_version(),
            "platform": platform.platform(),
        },
        "tests_executed": result.testsRun,
        "outcomes": outcomes,
        "qualified": all(o.ok for o in outcomes if o.status != "manual"),
    }
