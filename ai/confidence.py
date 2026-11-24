"""How much should you trust this answer — and the honest way to say "I don't know".

The system already knew. It just never told anybody.

``texttosql.runner`` counts ``attempts`` — how many times the model's SQL failed validation
and had to be corrected before one survived — and collects ``warnings``, which include the
post-execution sanity violations ("this aggregate looks impossible", "the join multiplied
your rows"). Both were computed on every request, put in the result dict, and **dropped on
the floor by the envelope.** So an answer the model got right first time and an answer it got
wrong twice arrived on screen looking *identical*, both wearing the same chip.

And that chip is the sharper problem. It reads **"Generated SQL — validated before it ran"**,
which is true and is heard as *"the answer is right"*. It means: read-only parse, table
allow-list, sanity checks. Not one of those says the query answered **the question that was
asked**. A model that writes a beautifully valid `SELECT avg(density)` when you asked about
*sulphur* passes every gate we have.

So: a **level with reasons**, never a percentage.

    A calibrated chip plus an honest refusal is a stronger trust move than a fake
    percentage — and a fake percentage is exactly the confidently-wrong number this
    product exists to eliminate. We are not going to put one on the screen and call it
    rigour.

Every reason below is a fact the system already had. Nothing here is estimated, and nothing
here is a model scoring itself — which would be the same coin, flipped twice.
"""
from __future__ import annotations

# Ordered worst → best; the level is the WORST thing that is true about the answer.
NONE = "none"              # we could not answer at all
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CERTIFIED = "certified"    # not a probability — a fact about which path ran

_LABEL = {
    CERTIFIED: "Certified",
    HIGH: "Confident",
    MEDIUM: "Check this",
    LOW: "Low confidence",
    NONE: "I don't know",
}


def grade(result: dict) -> dict:
    """Turn what the runner already knows into something a chemist can act on."""
    if not result or not result.get("understood", False):
        return {
            "level": NONE,
            "label": _LABEL[NONE],
            "reasons": ["I could not turn that question into a query I trust."],
            "trust": "Do not treat anything below as an answer.",
        }

    path = str(result.get("path") or "")
    reasons: list[str] = []

    # ---- the certified path -------------------------------------------------
    if path.startswith("certified_metric"):
        # The model wrote none of this SQL. That is not a confidence estimate, it is a fact
        # about the route, and it is the strongest thing we can say about any answer.
        out = {
            "level": CERTIFIED,
            "label": _LABEL[CERTIFIED],
            "reasons": ["This came from a pre-approved, QA-reviewed query. "
                        "The language model wrote none of it."],
            "trust": "The number is as reliable as the definition behind it.",
        }
        _add_freshness(out, result)
        return out

    # ---- the generated path -------------------------------------------------
    attempts = _attempt_count(result)
    warnings = [w for w in (result.get("warnings") or []) if w]
    rows = result.get("row_count")

    level = HIGH

    if attempts > 1:
        # The evidence we were throwing away. The model wrote SQL that did not survive
        # validation, and was corrected. Sometimes that correction is fine. Sometimes it is
        # the model flailing at a question it does not understand, and the validator only
        # ever checks that the SQL is *safe* — never that it is *right*.
        level = MEDIUM
        reasons.append(
            f"The model's first {attempts - 1} "
            f"{'query' if attempts == 2 else 'queries'} failed validation and had to be "
            f"corrected. It was working the question out as it went.")

    if warnings:
        level = MEDIUM
        for w in warnings[:3]:
            reasons.append(str(w))

    if rows == 0:
        # The ambiguity that matters, and which nothing in the product currently
        # distinguishes: "there is nothing to find" and "I asked the wrong question" produce
        # the identical empty table.
        level = LOW
        reasons.append(
            "The query returned no rows. That may mean there is genuinely nothing to find "
            "— or that the query asked something other than what you meant.")

    if attempts > 1 and warnings:
        level = LOW

    if level == HIGH:
        reasons.append("The model wrote this query first time, and it passed every check.")

    out = {
        "level": level,
        "label": _LABEL[level],
        "reasons": reasons,
        "trust": _TRUST[level],
    }
    _add_freshness(out, result)
    return out


_TRUST = {
    HIGH: "Read the SQL if the number matters. It is generated, not certified.",
    MEDIUM: "Read the SQL before you rely on this.",
    LOW: "Do not rely on this without checking the SQL yourself.",
}


def _attempt_count(result) -> int:
    """`attempts` is an int on the success path and a list of failed tries on the give-up
    path. Reading it as one shape everywhere is how it would silently become nonsense."""
    a = result.get("attempts")
    if isinstance(a, int):
        return a
    if isinstance(a, list):
        return len(a) + 1
    return 1


def _add_freshness(out: dict, result: dict) -> None:
    """Stale data does not make a query wrong; it makes the ANSWER wrong. It belongs here."""
    if result.get("freshness") == "stale":
        out["reasons"].append(
            result.get("freshness_detail")
            or "The data behind this is older than its refresh window.")
        if out["level"] in (HIGH, CERTIFIED):
            out["level"] = MEDIUM
            out["label"] = _LABEL[MEDIUM]
            out["trust"] = ("The query is sound; the DATA is stale. The number was true "
                            "at the timestamp shown, not now.")


# ---------------------------------------------------------------------------
# "I can't answer that — but here is what I CAN"
# ---------------------------------------------------------------------------
def what_i_can_answer(datasource, limit: int = 8) -> list[dict]:
    """The certified metrics available on this source.

    A refusal that stops at "I don't know" is a dead end and, in a demo, an embarrassment.
    A refusal that says *"not that — but I can tell you these, exactly, from approved
    SQL"* is the product's whole argument, made at the moment it is most persuasive.
    """
    if datasource is None:
        return []

    from semantics.models import CertifiedMetric

    return [
        {"key": m.key, "name": m.name, "description": m.description}
        for m in CertifiedMetric.objects.filter(datasource=datasource).order_by("name")[:limit]
    ]
