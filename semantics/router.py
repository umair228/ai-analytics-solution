"""Route a question to a certified metric (run trusted, curated SQL with no LLM)
or None (fall through to the validated long-tail text-to-SQL).

HIGH-PRECISION BY DESIGN. The certified-metric path bypasses the LLM *and* the
validator, so a wrong match is a confidently-wrong deterministic answer — far
worse than a long-tail miss (which is validated and self-repairing). A false
negative just defers to the long-tail, so we only fire when:

  1. one of the metric's curated synonyms appears as a CONTIGUOUS substring of the
     question (not an order-independent bag-of-words), AND
  2. the question adds no extra content words beyond that synonym — i.e. it carries
     no additional constraint (a time window, a product filter, "trend", "overall")
     that the fixed metric SQL would silently ignore.

Anything fuzzier is handled, correctly and with constraints honored, by the
long-tail path. Add synonyms to the YAML to widen the canonical phrasings.
"""
from __future__ import annotations

import re

from .text import tokenize

# ---------------------------------------------------------------------------
# The gate is an ALLOW-list, not a deny-list. That is the whole safety property.
#
# It used to be a deny-list: fire unless the question adds a word from a fixed set of
# known constraints. But a deny-list can only refuse the constraints somebody thought
# of, and a constraint nobody thought of therefore FIRES. The routing golden set caught
# exactly that: CONSTRAINT_WORDS contained "recent" but not "recently", so
#
#     "which product is tested most recently"
#
# matched the synonym "which product is tested most", found no denied word, and
# cheerfully returned the ALL-TIME most-tested product — a different question — with a
# ✓ Certified badge on it. The docstring above already promised the correct rule ("the
# question adds no extra content words beyond that synonym"); the code implemented a
# weaker one, and the gap is where the bug lived.
#
# So: defer on ANY extra word we do not positively recognise as harmless. An unknown
# word now costs a fall-through to the validated long-tail path (cheap, correct, a
# little slower) instead of a confidently-wrong deterministic answer (uncapped).
# ---------------------------------------------------------------------------

# Topic nouns that name what the metric is already about. They add no constraint the
# fixed SQL could fail to honour, so they are safe to ignore.
SAFE_EXTRA = {
    # Topic nouns — they name what the metric is already about.
    "egpc", "lab", "labs", "laboratory", "plant", "refinery", "site", "unit",
    "product", "products", "test", "tests", "testing", "sample", "samples",
    "result", "results", "analysis", "analyses", "analyte", "reading", "readings",
    "operator", "operators", "analyst", "analysts", "technician", "technicians",
    "spec", "specification", "average", "mean", "overall", "total", "range",
    "data", "database", "lims", "system", "there", "please", "us", "we",
    # LIMS lifecycle verbs. A sample is "registered", "logged", "reported" — none of
    # these narrows the question, they just describe what a sample *is*. Every one of
    # them was added because the allow-list refused a legitimate question without it:
    # a fail-safe gate is supposed to cost recall until you teach it, and this is the
    # bill. Cheap, and paid once.
    "registered", "logged", "recorded", "reported", "entered", "received",
    "performed", "conducted", "measured", "taken", "run",
}

# Kept for the EXPLANATION, not the gate: when we defer, naming the offending word is
# far more useful than "no match". Every one of these is, by construction, also absent
# from SAFE_EXTRA — so the allow-list already refuses them.
CONSTRAINT_WORDS = {
    "trend", "trends", "over", "time", "month", "months", "monthly", "year", "years",
    "yearly", "annual", "annually", "daily", "weekly", "quarter", "quarterly",
    "last", "past", "recent", "recently", "latest", "newest", "oldest", "current",
    "currently", "since", "between", "before", "after", "above", "below", "under",
    "greater", "less", "than", "vs", "versus", "compare", "comparison", "change",
    "growth", "forecast", "predict", "prediction", "projection", "project",
    "today", "yesterday", "week", "ytd", "wow", "mom", "yoy", "now", "first",
}

assert not (CONSTRAINT_WORDS & SAFE_EXTRA), (
    "A word cannot be both a constraint and harmless — the allow-list would let it "
    "through and the certified metric would silently ignore it."
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def blocking_words(question, synonym) -> set:
    """The extra words that stop this synonym from firing. Empty set == may fire."""
    return tokenize(question) - tokenize(synonym) - SAFE_EXTRA


def match_certified_metric(layer, question):
    """Return the most specific certified metric whose canonical phrasing appears in
    the question and which adds no unrecognised word, or None."""
    if not layer.metrics:
        return None
    qn = _norm(question)

    best = None
    best_specificity = -1
    # Only certified/live metrics answer as certified; drafts and deprecated ones are
    # documentation until their owner signs them off (the KPI lifecycle, P5).
    for metric in (m for m in layer.metrics if getattr(m, "offered", True)):
        for syn in metric.synonyms:
            sn = _norm(syn)
            if not sn or sn not in qn:                          # contiguous phrase hit
                continue
            if blocking_words(question, syn):                   # anything unknown → defer
                continue
            specificity = len(tokenize(syn))                    # prefer the most specific synonym
            if specificity > best_specificity:
                best, best_specificity = metric, specificity
    return best
