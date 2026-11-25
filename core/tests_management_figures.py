"""Pareto and bullet figures for the board PDF: the same cases as the board's own tests
(interactive-analytics-dse src/lib/pareto.test.mjs and bullet.test.mjs), so the PDF and
the screen say the same thing."""
from django.test import SimpleTestCase

from core import management_figures as mf
from core.board_render import format_value

NCRS = [{"name": "Dimensional", "value": 40}, {"name": "Weld defect", "value": 120},
        {"name": "Paint", "value": 20}, {"name": "Material", "value": 60},
        {"name": "Documentation", "value": 10}, {"name": "Damage", "value": 50}]


class ParetoTests(SimpleTestCase):
    def test_biggest_first_with_shares_and_running_total(self):
        p = mf.pareto_bars(NCRS)
        self.assertEqual(p["total"], 300)
        self.assertEqual([b["name"] for b in p["bars"]],
                         ["Weld defect", "Material", "Damage", "Dimensional", "Paint", "Documentation"])
        self.assertEqual(p["bars"][0]["share"], 40)
        self.assertEqual(p["bars"][1]["cumulative"], 60)
        self.assertEqual(p["bars"][-1]["cumulative"], 100)

    def test_vital_few_reach_the_threshold(self):
        p = mf.pareto_bars(NCRS)
        self.assertEqual(p["vital"], 4)
        self.assertEqual([b["vital"] for b in p["bars"]], [True, True, True, True, False, False])
        self.assertEqual(mf.pareto_bars(NCRS, threshold=60)["vital"], 2)

    def test_long_tail_folds_into_all_others(self):
        many = [{"name": f"C{i}", "value": 100 - i * 5} for i in range(14)]
        p = mf.pareto_bars(many, limit=6)
        self.assertEqual(len(p["bars"]), 6)
        self.assertEqual(p["count"], 14)
        self.assertEqual(p["bars"][-1]["name"], "All others (9)")
        self.assertFalse(p["bars"][-1]["vital"])
        self.assertEqual(sum(b["value"] for b in p["bars"]), p["total"])

    def test_vital_few_past_the_bars_shown(self):
        rows = ([{"name": f"Big{i}", "value": 100} for i in range(5)]
                + [{"name": f"Tail{i:02d}", "value": 10} for i in range(15)])
        p = mf.pareto_bars(rows, limit=4)
        self.assertEqual(p["reach"], 7)
        self.assertEqual([b["vital"] for b in p["bars"]], [True, True, True, False])
        self.assertEqual(mf.pareto_summary(p, items="suppliers", what="the spend"),
                         f"The top 3 of 20 suppliers make up {mf.js_round(p['vital_share'])}% of the spend; "
                         "it takes 7 to reach 80%.")

    def test_blanks_zeros_and_text_are_left_out(self):
        p = mf.pareto_bars([{"name": "", "value": 5}, {"name": None, "value": 5}, {"name": "A", "value": 0},
                            {"name": "B", "value": -3}, {"name": "C", "value": "x"},
                            {"name": "D", "value": "4"}, {"name": "D", "value": 6}, {"name": "E", "value": 2}])
        self.assertEqual([(b["name"], b["value"]) for b in p["bars"]], [("D", 10), ("E", 2)])
        self.assertEqual(mf.pareto_bars([])["bars"], [])

    def test_the_finding_in_words(self):
        w = {"items": "categories", "what": "the NCRs"}
        skewed = mf.pareto_bars([{"name": n, "value": v} for n, v in
                                 [("A", 70), ("B", 15), ("C", 5), ("D", 5), ("E", 5)]])
        self.assertEqual(mf.pareto_summary(skewed, **w), "2 of 5 categories make up 85% of the NCRs.")
        one = mf.pareto_bars([{"name": "A", "value": 90}, {"name": "B", "value": 5}, {"name": "C", "value": 5}])
        self.assertEqual(mf.pareto_summary(one, **w), "1 of 3 categories makes up 90% of the NCRs.")
        flat = mf.pareto_bars([{"name": n, "value": v} for n, v in [("A", 26), ("B", 25), ("C", 25), ("D", 24)]])
        self.assertEqual(mf.pareto_summary(flat, **w),
                         "Spread out: it takes 4 of 4 categories to reach 80% of the NCRs.")
        self.assertIsNone(mf.pareto_summary(mf.pareto_bars([]), **w))


class BulletTests(SimpleTestCase):
    def test_scale_ends_on_a_round_number(self):
        self.assertEqual(mf.nice_ceil(111), 120)
        self.assertEqual(mf.nice_ceil(2.625), 3)
        self.assertAlmostEqual(mf.nice_ceil(1.05), 1.2)
        self.assertEqual(mf.scale_max([72, 90, 80], "%"), 100)
        self.assertEqual(mf.scale_max([82, 85, 100], "%"), 120)
        self.assertEqual(mf.scale_max([2.16, 2.5, None], "%"), 3)
        self.assertAlmostEqual(mf.scale_max([0.944, 1, 0.9], "ratio"), 1.2)
        self.assertEqual(mf.position(150, 120), 100)
        self.assertIsNone(mf.position(None, 120))

    def test_grey_zones(self):
        self.assertEqual(mf.bullet_bands(target=90, floor=80, maximum=100),
                         [{"from": 0, "to": 80, "level": "poor"}, {"from": 80, "to": 90, "level": "fair"}])
        self.assertEqual(mf.bullet_bands(target=2, floor=3, direction="lower_better", maximum=4),
                         [{"from": 50, "to": 75, "level": "fair"}, {"from": 75, "to": 100, "level": "poor"}])
        self.assertEqual(mf.bullet_bands(target=None, maximum=3), [])

    def test_verdict(self):
        otd = {"target": 90, "floor": 80, "unit": "%"}
        self.assertEqual(mf.bullet_verdict(value=92, **otd), {"tone": "good", "text": "On target"})
        self.assertEqual(mf.bullet_verdict(value=85, **otd), {"tone": "watch", "text": "5 pts below target"})
        self.assertEqual(mf.bullet_verdict(value=72, **otd), {"tone": "act", "text": "18 pts below target"})
        trir = {"target": 2, "floor": 3, "unit": "ratio", "direction": "lower_better"}
        self.assertEqual(mf.bullet_verdict(value=2.42, **trir), {"tone": "watch", "text": "0.42 over target"})
        self.assertEqual(mf.bullet_verdict(value=1.9, **trir)["text"], "Within target")
        self.assertIsNone(mf.bullet_verdict(value=None, target=90))
        self.assertIsNone(mf.bullet_verdict(value=80, target=""))
        cost = {"target": 55, "unit": "%", "direction": "lower_better", "gap": "relative",
                "words": {"miss": "spent {gap} more than earned", "met": "within the value earned"}}
        self.assertEqual(mf.bullet_verdict(value=64, **cost)["text"], "spent 16% more than earned")
        self.assertEqual(mf.gap_text(12, 10, fmt=format_value, unit="SAR"), "SAR 2")

    def test_delta(self):
        self.assertEqual(mf.bullet_delta(72, 73.9, unit="%"), {"text": "▼ 1.9 pts", "sense": "bad"})
        self.assertEqual(mf.bullet_delta(2.42, 2.57, unit="ratio", direction="lower_better"),
                         {"text": "▼ 0.15", "sense": "good"})
        self.assertEqual(mf.bullet_delta(5, 5), {"text": "no change", "sense": "neutral"})
        self.assertIsNone(mf.bullet_delta(5, None))
        self.assertEqual(mf.bullet_delta(2.16, 2.19, unit="%", direction="lower_better")["text"], "▼ 0.03 pts")
        self.assertEqual(mf.bullet_delta(0.941, 0.936, unit="ratio")["text"], "▲ 0.005")
        self.assertEqual(mf.bullet_delta(71, 70, unit="%")["text"], "▲ 1 pt")
        self.assertEqual(mf.bullet_delta(0.944, 0.949, unit="ratio", fmt=format_value)["text"], "▼ 0.005")

    def test_names_wrap_under_their_bar(self):
        self.assertEqual(mf.wrap_name("Plates supplier 01", 11), ["Plates", "supplier 01"])
        self.assertEqual(mf.wrap_name("Plates supplier 01", 10), ["Plates", "suppli… 01"])
        self.assertEqual(mf.wrap_name("Procedure not followed", 12), ["Procedure", "not followed"])
        self.assertEqual(mf.wrap_name("Weld", 9), ["Weld"])
        self.assertEqual(mf.wrap_name("Documentation", 8), ["Documen…"])
