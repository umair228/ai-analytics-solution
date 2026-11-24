"""Render the Validation Summary Report.

Two things this document does that most validation summaries do not:

* **It says what it did NOT prove.** Manual items appear as OPERATOR TO VERIFY, not
  as a quiet pass. Unsigned PQ thresholds appear as SIGN-OFF REQUIRED, not as met.
  A summary that only lists what went well is a sales document.
* **It names the evidence.** Every requirement points at the test that verified it, by
  import path, so an assessor can re-run it themselves rather than take our word.
"""
from __future__ import annotations

from .requirements import PQ

STATUS = {
    "passed": ("PASS", "✅"),
    "failed": ("FAIL", "❌"),
    "missing": ("MATRIX ROT", "❌"),
    "manual": ("OPERATOR TO VERIFY", "⚠️"),
}


def render(evidence, fmt="md") -> str:
    return _html(evidence) if fmt == "html" else _md(evidence)


def _md(e) -> str:
    out = e["outcomes"]
    auto = [o for o in out if o.requirement.manual == ""]
    manual = [o for o in out if o.requirement.manual]
    env = e["environment"]

    L = []
    L.append("# Validation Summary Report")
    L.append("")
    L.append("**System:** DIS — Decision Intelligence Suite  ")
    L.append(f"**Generated:** {e['generated_at']}  ")
    L.append(f"**Environment:** Python {env['python']}, Django {env['django']}, "
             f"{env['platform']}  ")
    L.append(f"**Automated tests executed:** {e['tests_executed']}")
    L.append("")
    L.append("> **This system is NOT yet validated.** This report records the "
             "Operational Qualification, which is complete for the automated scope. "
             f"{len(manual)} requirements cannot be proven by any automated test and "
             "must be verified by the operator on the installed system; "
             f"{len(PQ)} performance thresholds require sign-off. Both are listed below, "
             "and neither is a pass.")
    L.append("")
    L.append("## Approach")
    L.append("")
    L.append("The traceability matrix is executable code (`validation/requirements.py`), "
             "not a table in a document. Each requirement names the automated tests that "
             "verify it, by import path; `manage.py qualify` runs them and **fails if a "
             "named test does not exist**. A matrix that cannot rot is the only kind "
             "worth having.")
    L.append("")
    L.append("This follows GAMP 5 (2nd ed.) and the CSA position that automated tests "
             "are legitimate — and better — qualification evidence than a screenshot of "
             "someone clicking: they are repeatable, dated, and they run again on every "
             "change.")
    L.append("")

    # ---- OQ ----
    L.append("## 1. Operational Qualification — executed")
    L.append("")
    L.append("| ID | Risk | Requirement | Result | Tests | Evidence |")
    L.append("|---|---|---|---|---|---|")
    for o in auto:
        r = o.requirement
        label, icon = STATUS[o.status]
        ev = "<br>".join(f"`{v}`" for v in r.verified_by)
        L.append(f"| **{r.id}** | {r.risk} | {r.statement} | {icon} {label} | "
                 f"{o.tests_run} | {ev} |")
        if o.failures:
            L.append(f"| | | | | | ❌ failed: {', '.join(f'`{f}`' for f in o.failures)} |")
        if o.missing:
            L.append(f"| | | | | | ❌ **the matrix names a test that does not exist:** "
                     f"{', '.join(f'`{m}`' for m in o.missing)} |")
    L.append("")

    L.append("### Why each requirement exists")
    L.append("")
    for o in auto:
        r = o.requirement
        L.append(f"**{r.id} — {r.statement}**  ")
        L.append(f"{r.rationale}")
        if r.note:
            L.append(f"  ")
            L.append(f"> ⚠️ {r.note}")
        L.append("")

    # ---- IQ / manual ----
    L.append("## 2. Installation Qualification — OPERATOR TO VERIFY")
    L.append("")
    L.append("No unit test can prove that TLS is terminated, that a database grant was "
             "applied, or that a backup restores. These are **not passes**. Each must be "
             "executed on the installed system and its evidence attached.")
    L.append("")
    L.append("| ID | Risk | Requirement | Procedure | Evidence attached | Verified by | Date |")
    L.append("|---|---|---|---|---|---|---|")
    for o in manual:
        r = o.requirement
        L.append(f"| **{r.id}** | {r.risk} | {r.statement} | {r.manual} | ☐ | ______ | ______ |")
    L.append("")

    # ---- PQ ----
    L.append("## 3. Performance Qualification — SIGN-OFF REQUIRED")
    L.append("")
    L.append("These are the numbers the laboratory will be held to in an audit. They are "
             "**proposed with reasoning, not decided**. Inventing an acceptance threshold "
             "on the laboratory's behalf would be exactly the kind of confident, "
             "unfounded number this system exists to eliminate.")
    L.append("")
    for p in PQ:
        L.append(f"### {p.id} — {p.statement}")
        L.append("")
        L.append(f"- **Measured by:** `{p.measured_by}`")
        L.append(f"- **Observed today:** {p.observed}")
        L.append(f"- **Proposed:** {p.proposed}")
        L.append(f"- **Why it matters:** {p.rationale}")
        L.append(f"- **Agreed threshold:** `[DECISION REQUIRED]`  ")
        L.append(f"- **Signed off by:** ______________________  **Date:** ____________")
        L.append("")

    # ---- Sign-off ----
    L.append("## 4. Conclusion and sign-off")
    L.append("")
    passed = sum(1 for o in auto if o.status == "passed")
    L.append(f"- Operational Qualification: **{passed} of {len(auto)}** requirements "
             f"verified by {e['tests_executed']} automated tests.")
    L.append(f"- Installation Qualification: **0 of {len(manual)}** verified — "
             f"operator action outstanding.")
    L.append(f"- Performance Qualification: **0 of {len(PQ)}** thresholds agreed.")
    L.append("")
    L.append("**The system is not validated until sections 2 and 3 are complete.**")
    L.append("")
    L.append("| Role | Name | Signature | Date |")
    L.append("|---|---|---|---|")
    L.append("| Prepared by (System owner) | | | |")
    L.append("| Reviewed by (QA) | | | |")
    L.append("| Approved by (Quality Assurance Manager) | | | |")
    L.append("")
    return "\n".join(L)


def _html(e) -> str:
    body = _md(e)
    # Deliberately not a markdown renderer: this document is read for its content, and a
    # <pre> block cannot silently drop a row the way a half-implemented parser can.
    from html import escape
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>DIS — Validation Summary</title>"
        "<style>body{font:13px/1.6 -apple-system,Segoe UI,sans-serif;margin:32px;"
        "color:#0f172a;max-width:1100px}pre{white-space:pre-wrap;word-wrap:break-word}"
        "</style></head><body><pre>" + escape(body) + "</pre></body></html>"
    )
