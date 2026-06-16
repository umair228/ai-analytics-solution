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

# Words that change the SHAPE of the answer (a time series, a filter, a comparison)
# which a fixed metric SQL cannot honor. If the question adds one of these beyond
# the matched synonym, defer to the (constraint-aware) long-tail path. Plain topic
# words ("product", "EGPC", "results") are fine — they don't change the metric.
CONSTRAINT_WORDS = {
    "trend", "trends", "over", "time", "month", "months", "monthly", "year", "years",
    "yearly", "annual", "annually", "daily", "weekly", "quarter", "quarterly",
    "last", "past", "recent", "since", "between", "before", "after", "above",
    "below", "under", "greater", "less", "than", "vs", "versus", "compare",
    "comparison", "change", "growth", "forecast", "predict", "projection",
    "today", "yesterday", "week", "quarterly", "ytd", "wow", "mom", "yoy",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match_certified_metric(layer, question):
    """Return the most specific certified metric whose canonical phrasing appears
    in the question with no added shape/time/filter constraint, or None."""
    if not layer.metrics:
        return None
    qn = _norm(question)
    q_tokens = tokenize(question)

    best = None
    best_specificity = -1
    for metric in layer.metrics:
        for syn in metric.synonyms:
            sn = _norm(syn)
            if not sn or sn not in qn:                          # contiguous phrase hit
                continue
            extra = q_tokens - tokenize(syn)
            if extra & CONSTRAINT_WORDS:                        # adds a constraint → defer
                continue
            specificity = len(tokenize(syn))                    # prefer the most specific synonym
            if specificity > best_specificity:
                best, best_specificity = metric, specificity
    return best
