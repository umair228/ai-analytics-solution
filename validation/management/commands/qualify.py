"""Execute the Operational Qualification and emit the Validation Summary.

    python manage.py qualify                 # run it, print the summary
    python manage.py qualify --out report.md # write the summary
    python manage.py qualify --matrix        # just check the matrix hasn't rotted

Exits non-zero if any requirement fails or names a test that no longer exists — so it
can sit in CI, which is the only place a validation artefact stays true.
"""
from django.core.management.base import BaseCommand

from validation import report, runner


class Command(BaseCommand):
    help = "Run the OQ traceability matrix and produce the Validation Summary."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Write the summary to this file (.md or .html).")
        parser.add_argument("--matrix", action="store_true",
                            help="Only verify that every named test exists; run nothing.")
        parser.add_argument(
            "--json", nargs="?", const=True, default=False, metavar="PATH",
            help=("Also write the machine-readable OQ report (default: "
                  "validation/oq_report.json). This is the artefact the /validation page "
                  "serves — the OQ is evidence about the build that was qualified, not "
                  "something production re-runs."))

    def handle(self, *args, **opts):
        if opts["matrix"]:
            return self._matrix_only()

        self.stdout.write("Running the Operational Qualification…\n")
        evidence = runner.run(verbosity=0)

        for o in evidence["outcomes"]:
            r = o.requirement
            if o.status == "manual":
                mark, style = "MANUAL", self.style.WARNING
            elif o.status == "passed":
                mark, style = f"PASS ({o.tests_run})", self.style.SUCCESS
            elif o.status == "missing":
                mark, style = "MATRIX ROT", self.style.ERROR
            else:
                mark, style = f"FAIL ({len(o.failures)})", self.style.ERROR
            self.stdout.write(f"  {r.id:<9}{r.risk.upper():<7}{style(mark):<26}"
                              f"{r.statement[:50]}")
            for m in o.missing:
                self.stdout.write(self.style.ERROR(
                    f"           -> no such test: {m}"))
            for f in o.failures:
                self.stdout.write(self.style.ERROR(f"           -> FAILED: {f}"))

        text = report.render(evidence,
                             fmt="html" if (opts["out"] or "").endswith(".html") else "md")
        if opts["out"]:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(f"\nSummary written to {opts['out']}")

        if opts["json"]:
            from validation import artifact

            path = artifact.write(
                evidence, None if opts["json"] is True else opts["json"])
            self.stdout.write(f"OQ report written to {path}")

        n_manual = sum(1 for o in evidence["outcomes"] if o.status == "manual")
        self.stdout.write(
            f"\n{evidence['tests_executed']} automated tests executed across "
            f"{len(evidence['outcomes']) - n_manual} requirements.")
        self.stdout.write(self.style.WARNING(
            f"{n_manual} requirement(s) CANNOT be qualified by automated test and are "
            f"marked OPERATOR TO VERIFY. They are not a pass."))
        self.stdout.write(self.style.WARNING(
            "4 PQ acceptance thresholds require sign-off before this system is "
            "validated. They are proposed, not decided."))

        if not evidence["qualified"]:
            self.stderr.write(self.style.ERROR("\nOQ NOT PASSED."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nOQ PASSED (automated scope)."))

    def _matrix_only(self):
        """Cheap CI gate: does every requirement still name a test that exists?"""
        from django.test.runner import DiscoverRunner

        from validation.requirements import ALL

        bad = []
        r = DiscoverRunner(verbosity=0, interactive=False)
        for req in ALL:
            if req.manual:
                continue
            for label in req.verified_by:
                try:
                    if r.build_suite([label]).countTestCases() == 0:
                        bad.append((req.id, label, "matches no test"))
                except Exception as exc:  # noqa: BLE001
                    bad.append((req.id, label, str(exc).splitlines()[0]))

        for req_id, label, why in bad:
            self.stderr.write(self.style.ERROR(f"  {req_id}: {label} — {why}"))
        if bad:
            self.stderr.write(self.style.ERROR(
                f"\n{len(bad)} broken link(s). The traceability matrix asserts a "
                f"verification that does not exist."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            "Traceability matrix intact — every requirement names a test that exists."))
