"""The shipped PMSv2 semantic layer (semantics/specs/fmms.yaml): every static rule,
owner and status discipline, no invented thresholds, no money, no product name."""
from pathlib import Path

import yaml
from django.test import SimpleTestCase

from semantics.spec import load_yaml, validate_health, validate_layer, validate_metrics

PMSV2_SPEC = Path(__file__).resolve().parent / "specs" / "fmms.yaml"


def all_errors(spec):
    return validate_layer(spec) + validate_metrics(spec) + validate_health(spec)


class ShippedPmsv2SpecTests(SimpleTestCase):
    def setUp(self):
        self.spec = load_yaml(PMSV2_SPEC)

    def test_it_passes_every_static_rule(self):
        self.assertEqual(all_errors(self.spec), [])

    def test_every_kpi_is_observed_and_awaits_owner_sign_off(self):
        for m in self.spec["metrics"]:
            self.assertTrue(m.get("kpi_id") and m.get("observe"), m["key"])
            self.assertEqual(m.get("status"), "parity_checked", m["key"])
            self.assertTrue(m.get("owner") and m.get("unit"), m["key"])

    def test_no_threshold_is_invented(self):
        """Only the freshness rule (DEC-05) is a threshold; every other number is shown,
        not graded, until its owner agrees one."""
        graded = [t["key"] for t in self.spec["health"]
                  if any(t.get(b) is not None for b in ("target", "warn", "critical"))]
        self.assertEqual(graded, ["data_freshness_hours"])
        self.assertFalse(any(m.get("thresholds") for m in self.spec["metrics"]))

    def test_no_money_and_no_product_name_in_it(self):
        text = PMSV2_SPEC.read_text()
        self.assertNotIn("rpt_fin", text.replace("rpt_fin is read only", ""))
        rendered = yaml.safe_dump({k: self.spec[k] for k in ("notes", "metrics", "health")})
        self.assertNotIn("FMMS", rendered)
