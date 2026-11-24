"""Score the certified-metric router against the routing golden set.

This is the accuracy gate CI has never had, and it needs **no LLM and no database** —
the router is a pure function of the question and the curated layer. It can therefore
run on every push, which is the only place an accuracy claim stays true.

It is also the accuracy that matters most, and the reason is asymmetry:

    A FALSE NEGATIVE (a metric that should fire and doesn't) is cheap. The question
    falls through to the long-tail path, which is validated, constraint-aware and
    self-repairing. The user gets a correct answer, slightly slower.

    A FALSE POSITIVE (a metric that fires when it should not) is expensive. The
    certified path bypasses the LLM *and* the validator, so the user gets a
    confidently-wrong deterministic answer with a ✓ Certified badge on it. Asking
    "how many results are out of spec LAST MONTH" and being handed the all-time
    total, stamped certified, is the worst failure this system can produce.

So the two rates are NOT weighted equally, and the report says so. A wrong-metric
match is counted as a false positive, not merely a miss — it is the same failure.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

from .layer import Layer, LMetric
from .router import match_certified_metric
from .spec import load_yaml


def golden_path() -> Path:
    return Path(settings.BASE_DIR) / "eval" / "routing_golden.jsonl"


def load_cases(path=None) -> list[dict]:
    p = Path(path) if path else golden_path()
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def layer_from_spec(spec_path=None) -> Layer:
    """Build the runtime layer straight from the shipped YAML — no database needed."""
    p = spec_path or (Path(settings.BASE_DIR) / "semantics" / "specs" / "egpc_dev.yaml")
    spec = load_yaml(p)
    metrics = [
        LMetric(key=m["key"], name=m.get("name", m["key"]), sql=m["sql"],
                row_limit=m.get("limit"), synonyms=m.get("synonyms") or [])
        for m in spec["metrics"]
    ]
    return Layer(metrics=metrics)


def evaluate(cases=None, layer=None) -> dict:
    cases = cases or load_cases()
    layer = layer or layer_from_spec()

    results = []
    for c in cases:
        got = match_certified_metric(layer, c["question"])
        got_key = got.key if got else None
        expected = c.get("expect")

        if expected is None and got_key is None:
            outcome = "correct_defer"
        elif expected is None and got_key is not None:
            # Fired when it should have deferred. The dangerous one.
            outcome = "false_positive"
        elif expected is not None and got_key is None:
            outcome = "false_negative"
        elif got_key == expected:
            outcome = "correct_route"
        else:
            # Fired the WRONG metric. This is a false positive, not a near-miss:
            # the user still gets a confidently-wrong certified answer.
            outcome = "wrong_metric"

        results.append({**c, "got": got_key, "outcome": outcome})

    n = len(results)
    counts = {k: sum(1 for r in results if r["outcome"] == k)
              for k in ("correct_route", "correct_defer", "false_positive",
                        "wrong_metric", "false_negative")}
    dangerous = counts["false_positive"] + counts["wrong_metric"]

    should_fire = sum(1 for r in results if r.get("expect") is not None)
    did_fire = sum(1 for r in results if r["got"] is not None)

    return {
        "n": n,
        "counts": counts,
        # Precision: of the times it fired, how often was it right? This is the number
        # that must be 1.0 — every point below it is a certified lie.
        "precision": (counts["correct_route"] / did_fire) if did_fire else 1.0,
        # Recall: of the questions a metric should have answered, how many did it?
        # Below 1.0 costs latency, not correctness.
        "recall": (counts["correct_route"] / should_fire) if should_fire else 1.0,
        "accuracy": (counts["correct_route"] + counts["correct_defer"]) / n if n else 1.0,
        "dangerous": dangerous,
        "results": results,
    }
