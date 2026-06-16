"""Tiny dependency-free tokenizer + overlap scorer shared by the retriever and
router. (A fine-tuned embedder over these same descriptors is the documented
future upgrade — see DIS_TalkToData_Plan.md §4; the scoring API stays the same.)
"""
from __future__ import annotations

import re

_STOP = {
    "the", "a", "an", "of", "for", "to", "in", "on", "by", "is", "are", "was",
    "were", "and", "or", "with", "which", "what", "show", "list", "give", "me",
    "how", "many", "much", "across", "per", "each", "all", "do", "does", "did",
    "have", "has", "this", "that", "these", "those", "our", "their", "it", "be",
    "value", "values", "number", "count",
}


def tokenize(text: str) -> set[str]:
    """Lower-cased alphanumeric tokens (and de-underscored sub-tokens), minus stopwords."""
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in raw if t and t not in _STOP and len(t) >= 2}


def phrase_hit(phrase: str, question_lower: str) -> bool:
    """True if a multi-word synonym phrase appears (loosely) in the question."""
    p = (phrase or "").strip().lower()
    if not p:
        return False
    if p in question_lower:
        return True
    # all words of the phrase present (order-independent) for short phrases
    words = [w for w in re.split(r"[^a-z0-9]+", p) if w and w not in _STOP]
    return bool(words) and all(w in question_lower for w in words)


def overlap_score(question_tokens: set[str], *texts: str) -> int:
    """How many distinct question tokens appear in the given descriptor texts."""
    bag = set()
    for t in texts:
        bag |= tokenize(t)
    return len(question_tokens & bag)
