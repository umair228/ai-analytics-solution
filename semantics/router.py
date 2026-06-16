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

# How many question content-words may remain after removing the matched synonym's
# words. 0 = the question must be essentially the canonical metric question.
MAX_EXTRA_TOKENS = 0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match_certified_metric(layer, question):
    """Return the most specific certified metric whose canonical phrasing the
    question matches with no extra constraints, or None."""
    if not layer.metrics:
        return None
    qn = _norm(question)
    q_content = tokenize(question)

    best = None
    best_specificity = -1
    for metric in layer.metrics:
        for syn in metric.synonyms:
            sn = _norm(syn)
            if not sn or sn not in qn:                 # require a contiguous phrase hit
                continue
            extra = q_content - tokenize(syn)
            if len(extra) > MAX_EXTRA_TOKENS:          # question adds a constraint → defer
                continue
            specificity = len(tokenize(syn))           # prefer the most specific synonym
            if specificity > best_specificity:
                best, best_specificity = metric, specificity
    return best
