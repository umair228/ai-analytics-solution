"""Build a Certificate of Analysis from the LIMS — with certified SQL, never the LLM.

The one thing this module exists to get right:

    LabWare's TEST table records what was ORDERED.
    LabWare's RESULT table records what came BACK.
    A certificate must reconcile them.

A test that was ordered and never reported is the dangerous case. The naive builder
lists the results it happens to find, sees no failures among them, and prints PASS —
which is a batch released without being fully tested, over a signature. So any test
without a result forces the verdict to INCOMPLETE, and the missing tests are printed
on the face of the document rather than quietly omitted.

The SQL is portable (``semantics.metric_sql``), so a certificate can be issued from the
LabWare source or from the replica without changing a line.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from querybuilder.executor import QueryError, assert_read_only
from semantics.metric_sql import render as render_sql
from texttosql.execution import execute_with_deadline

logger = logging.getLogger(__name__)


class CoaError(Exception):
    """The certificate cannot be built (unknown sample, unreachable LIMS)."""


SAMPLE_SQL = """
SELECT SAMPLE_NUMBER, TEXT_ID, PRODUCT, SAMPLE_TYPE, STATUS, IN_SPEC,
       LOGIN_DATE, DATE_COMPLETED
FROM {{table:SAMPLE}}
WHERE SAMPLE_NUMBER = :sample
"""

RESULTS_SQL = """
SELECT RESULT_NUMBER, ANALYSIS, NAME, NUMERIC_ENTRY, UNITS, IN_SPEC,
       ENTERED_BY, ENTERED_ON
FROM {{table:RESULT}}
WHERE SAMPLE_NUMBER = :sample
ORDER BY ANALYSIS, RESULT_NUMBER
"""

TESTS_SQL = """
SELECT TEST_NUMBER, ANALYSIS, STATUS
FROM {{table:TEST}}
WHERE SAMPLE_NUMBER = :sample
ORDER BY ANALYSIS
"""

STATUS_LABELS = {
    "A": "Authorized", "C": "Complete", "X": "Cancelled", "R": "Rejected",
    "U": "Unreceived", "I": "In Progress", "P": "Pending", "D": "Disposed",
}

# A test in one of these states was never going to produce a result, and holding a
# certificate open for it forever would be its own kind of wrong. It is still named on
# the document — "cancelled" is information a customer is entitled to — but it does not
# make the certificate incomplete.
CLOSED_TEST_STATES = {"X", "R", "D"}


def _rows(datasource, sql, sample):
    rendered = render_sql(sql, datasource)
    assert_read_only(rendered)
    payload = execute_with_deadline(datasource, rendered, params={"sample": str(sample)})
    cols = payload["columns"]
    return [dict(zip(cols, r)) if isinstance(r, (list, tuple)) else dict(r)
            for r in payload["rows"]]


def _get(row, key, default=None):
    for k in (key, key.lower(), key.upper()):
        if k in row:
            return row[k]
    return default


def _iso(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def build(datasource, sample_number) -> dict:
    """The certificate content — a snapshot, ready to be hashed and signed."""
    from replication.freshness import describe

    try:
        header = _rows(datasource, SAMPLE_SQL, sample_number)
        results = _rows(datasource, RESULTS_SQL, sample_number)
        ordered = _rows(datasource, TESTS_SQL, sample_number)
    except QueryError as exc:
        raise CoaError(f"Could not read sample {sample_number} from "
                       f"{datasource.name}: {exc}") from exc

    if not header:
        raise CoaError(f"Sample {sample_number} was not found in {datasource.name}.")
    h = header[0]

    reported = []
    for r in results:
        flag = str(_get(r, "IN_SPEC") or "").strip().upper()
        reported.append({
            "analysis": _get(r, "ANALYSIS"),
            "component": _get(r, "NAME"),
            "value": _get(r, "NUMERIC_ENTRY"),
            "units": _get(r, "UNITS"),
            "in_spec": flag or None,
            # "" and NULL are NOT a pass. An unadjudicated result is unadjudicated.
            "conforms": True if flag == "T" else False if flag == "F" else None,
            "analyst": _get(r, "ENTERED_BY"),
            "reported_at": _iso(_get(r, "ENTERED_ON")),
        })

    have = {str(r["analysis"]).upper() for r in reported if r["analysis"]}

    # Reconcile ordered vs reported. This is the whole point of the module.
    missing, cancelled = [], []
    for t in ordered:
        analysis = _get(t, "ANALYSIS")
        state = str(_get(t, "STATUS") or "").strip().upper()
        if not analysis:
            continue
        entry = {"analysis": analysis, "status": state,
                 "status_label": STATUS_LABELS.get(state, state or "Unknown")}
        if str(analysis).upper() in have:
            continue
        (cancelled if state in CLOSED_TEST_STATES else missing).append(entry)

    unadjudicated = [r for r in reported if r["conforms"] is None]
    failed = [r for r in reported if r["conforms"] is False]

    if failed:
        # Definitive: it already failed. Incompleteness cannot rescue it.
        verdict = "does_not_conform"
    elif missing or unadjudicated or not reported:
        verdict = "incomplete"
    else:
        verdict = "conforms"

    fresh = describe(datasource)
    return {
        "schema_version": 1,
        "sample": {
            "sample_number": str(_get(h, "SAMPLE_NUMBER")),
            "text_id": _get(h, "TEXT_ID") or "",
            "product": _get(h, "PRODUCT") or "",
            "sample_type": _get(h, "SAMPLE_TYPE") or "",
            "status": str(_get(h, "STATUS") or ""),
            "status_label": STATUS_LABELS.get(str(_get(h, "STATUS") or ""), ""),
            "logged_at": _iso(_get(h, "LOGIN_DATE")),
            "completed_at": _iso(_get(h, "DATE_COMPLETED")),
        },
        "results": reported,
        "tests_ordered": len(ordered),
        "tests_reported": len(reported),
        "missing_tests": missing,          # ordered, still open, never reported
        "cancelled_tests": cancelled,      # ordered, closed without a result
        "unadjudicated": [r["analysis"] for r in unadjudicated],
        "verdict": verdict,
        "verdict_reason": _reason(verdict, failed, missing, unadjudicated, reported),
        "provenance": {
            "datasource": {"id": datasource.id, "name": datasource.name},
            "generated_at": timezone.now().isoformat(),
            **fresh.as_provenance(),
        },
    }


def _reason(verdict, failed, missing, unadjudicated, reported) -> str:
    if verdict == "does_not_conform":
        names = ", ".join(str(r["analysis"]) for r in failed[:6])
        return (f"{len(failed)} result(s) are outside specification: {names}. "
                "This sample does NOT conform.")
    if verdict == "incomplete":
        bits = []
        if not reported:
            bits.append("no results have been reported for this sample")
        if missing:
            bits.append(f"{len(missing)} test(s) were ordered but never reported "
                        f"({', '.join(str(m['analysis']) for m in missing[:6])})")
        if unadjudicated:
            bits.append(f"{len(unadjudicated)} result(s) carry no in-spec decision "
                        f"({', '.join(str(a) for a in unadjudicated[:6])})")
        return ("This certificate CANNOT be issued: " + "; ".join(bits) +
                ". A sample cannot be certified as conforming until every test "
                "ordered against it has been reported and adjudicated.")
    return (f"All {len(reported)} reported result(s) are within specification, and "
            "every test ordered has been accounted for.")
