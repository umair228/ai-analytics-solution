"""Semantic-layer tests: pure retrieval/router/text scoring, plus an end-to-end
SQLite integration (load a spec → runtime layer → schema build → certified-metric
fast path → generated long-tail path → live-schema drift detection).
"""
import os
import tempfile
from unittest import mock

import pandas as pd
from sqlalchemy import create_engine
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from semantics.layer import LColumn, LJoin, LMetric, LTable, Layer, get_semantic_layer
from semantics.retrieval import select_tables
from semantics.router import match_certified_metric
from semantics.spec import SpecError, apply_spec, join_on, load_yaml, verify_spec
from semantics.text import overlap_score, phrase_hit, tokenize


def _layer():
    sample = LTable("SAMPLE", description="sample login register",
                    synonyms=["sample", "login"], columns={
                        "SAMPLE_NUMBER": LColumn("SAMPLE_NUMBER"),
                        "PRODUCT": LColumn("PRODUCT", synonyms=["product", "grade"]),
                        "IN_SPEC": LColumn("IN_SPEC", encoding="T=in-spec, F=off-spec")})
    result = LTable("RESULT", description="individual test results",
                    synonyms=["result", "reading"], columns={
                        "SAMPLE_NUMBER": LColumn("SAMPLE_NUMBER"),
                        "NUMERIC_ENTRY": LColumn("NUMERIC_ENTRY", needs_cast=True),
                        "ENTERED_BY": LColumn("ENTERED_BY", synonyms=["operator", "analyst"])})
    test = LTable("TEST", description="tests", columns={
        "INSTRUMENT": LColumn("INSTRUMENT", do_not_group=True)})
    return Layer(datasource_id=1, datasource_name="EGPC",
                 tables={"SAMPLE": sample, "RESULT": result, "TEST": test},
                 joins=[LJoin("RESULT", "SAMPLE", [["SAMPLE_NUMBER", "SAMPLE_NUMBER"]])],
                 metrics=[LMetric("oos_by_operator", "Off-spec by operator", "SELECT 1",
                                  synonyms=["off spec by operator"])])


class TextScoringTests(SimpleTestCase):
    def test_tokenize_drops_stopwords_and_splits(self):
        self.assertEqual(tokenize("worst out-of-spec rate by product"),
                         {"worst", "out", "spec", "rate", "product"})

    def test_phrase_hit_order_independent(self):
        self.assertTrue(phrase_hit("off spec rate by product",
                                   "which product has the worst off spec rate"))
        self.assertFalse(phrase_hit("turnaround time", "off spec by operator"))

    def test_overlap_score(self):
        # off, spec, operator all overlap ("by" is a stopword)
        self.assertEqual(overlap_score(tokenize("operator off spec"), "off spec by operator"), 3)
        self.assertEqual(overlap_score(tokenize("turnaround time"), "off spec by operator"), 0)


class JoinKeyTests(SimpleTestCase):
    def test_quoted_on(self):
        self.assertEqual(join_on({"left": "R", "right": "S", "on": [["A", "B"]]}), [["A", "B"]])

    def test_yaml_bool_on(self):
        # YAML 1.1 coerces a bare `on:` key to the boolean True
        self.assertEqual(join_on({"left": "R", "right": "S", True: [["A", "B"]]}), [["A", "B"]])

    def test_missing_on(self):
        self.assertEqual(join_on({"left": "R", "right": "S"}), [])


class EgpcSpecFileTests(SimpleTestCase):
    def test_shipped_egpc_yaml_is_well_formed(self):
        from django.conf import settings
        spec = load_yaml(os.path.join(settings.BASE_DIR, "semantics", "specs", "egpc_dev.yaml"))
        self.assertEqual(spec["datasource"], "EGPC_DEV")
        self.assertTrue(spec["joins"])
        for j in spec["joins"]:                       # the on-key quoting must hold
            self.assertTrue(join_on(j), f"join {j} resolved no ON columns")
            self.assertTrue(j.get("left") and j.get("right"))
        for m in spec["metrics"]:
            self.assertTrue(m.get("key") and m.get("sql"))


class RetrievalTests(SimpleTestCase):
    def test_picks_result_for_operator_question_and_join_partner(self):
        chosen = select_tables(_layer(), "off-spec results by operator", max_tables=8)
        self.assertIn("RESULT", chosen)
        self.assertIn("SAMPLE", chosen)  # pulled in via the certified join hop

    def test_picks_sample_for_product_question(self):
        self.assertIn("SAMPLE", select_tables(_layer(), "breakdown by product", max_tables=8))

    def test_no_signal_falls_back_to_tables(self):
        chosen = select_tables(_layer(), "zzz qqq", max_tables=8)
        self.assertTrue(chosen)  # never returns empty when tables exist

    def test_do_not_group_columns(self):
        self.assertEqual(_layer().do_not_group_columns(), {"INSTRUMENT"})


class RouterTests(SimpleTestCase):
    def test_matches_canonical_synonym(self):
        m = match_certified_metric(_layer(), "show me off spec by operator")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "oos_by_operator")

    def test_added_constraint_does_not_fire(self):
        # an extra constraint word ("year") the fixed metric can't honor → defer to long-tail
        self.assertIsNone(match_certified_metric(_layer(), "off spec by operator last year"))

    def test_non_contiguous_phrase_does_not_fire(self):
        # words present but not as the canonical phrase → no false fire
        self.assertIsNone(match_certified_metric(_layer(), "which operator logged the most samples"))

    def test_no_match_returns_none(self):
        self.assertIsNone(match_certified_metric(_layer(), "what is the flash point of diesel"))

    def test_no_metrics_returns_none(self):
        layer = _layer()
        layer.metrics = []
        self.assertIsNone(match_certified_metric(layer, "off spec by operator"))

    def test_topic_word_extra_still_fires(self):
        # plain topic words (egpc/lab) don't change the metric → still fires
        m = match_certified_metric(_layer(), "show me off spec by operator for the egpc lab")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "oos_by_operator")

    def test_shape_constraint_word_defers(self):
        # "trend"/"over time" change the answer shape → defer to long-tail
        self.assertIsNone(match_certified_metric(_layer(), "off spec by operator trend over time"))


class EgpcMetricRoutingTests(SimpleTestCase):
    """Guards the shipped egpc_dev.yaml synonyms against routing regressions."""

    def _layer_from_file(self):
        from django.conf import settings
        spec = load_yaml(os.path.join(settings.BASE_DIR, "semantics", "specs", "egpc_dev.yaml"))
        metrics = [LMetric(key=m["key"], name=m.get("name", m["key"]), sql=m["sql"],
                           synonyms=m.get("synonyms") or []) for m in spec["metrics"]]
        return Layer(metrics=metrics)

    def test_oos_rate_fires_on_golden_phrasing(self):
        m = match_certified_metric(
            self._layer_from_file(),
            "On which product is the out-of-specification rate highest across EGPC?")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "oos_rate_by_product")

    def test_sulphur_fires(self):
        m = match_certified_metric(
            self._layer_from_file(), "What is the average sulphur value for each product?")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "sulphur_by_product")

    def test_unrelated_question_defers(self):
        self.assertIsNone(match_certified_metric(
            self._layer_from_file(), "how many samples were logged last month"))

    def test_oos_total_routes(self):
        m = match_certified_metric(
            self._layer_from_file(), "How many results are out of specification across EGPC?")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "oos_total")

    def test_oos_by_test_routes(self):
        m = match_certified_metric(
            self._layer_from_file(),
            "On which test have the greatest number of samples gone out of specification across EGPC?")
        self.assertIsNotNone(m)
        self.assertEqual(m.key, "oos_by_test_count")

    def test_oos_metrics_do_not_cross_fire(self):
        # the rate question must hit the RATE metric, not the count/total ones
        layer = self._layer_from_file()
        self.assertEqual(
            match_certified_metric(
                layer, "On which product is the out-of-specification rate highest across EGPC?").key,
            "oos_rate_by_product")


class SemanticIntegrationTests(TestCase):
    def setUp(self):
        from connections.models import DataSource
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "egpc.sqlite")
        eng = create_engine(f"sqlite:///{self.path}")
        pd.DataFrame([
            {"SAMPLE_NUMBER": 1, "PRODUCT": "DIESEL", "IN_SPEC": "T"},
            {"SAMPLE_NUMBER": 2, "PRODUCT": "PROPANE", "IN_SPEC": "F"},
            {"SAMPLE_NUMBER": 3, "PRODUCT": "DIESEL", "IN_SPEC": "F"},
        ]).to_sql("SAMPLE", eng, index=False, if_exists="replace")
        pd.DataFrame([
            {"SAMPLE_NUMBER": 1, "NUMERIC_ENTRY": "10.5", "IN_SPEC": "T", "ENTERED_BY": "5227"},
            {"SAMPLE_NUMBER": 2, "NUMERIC_ENTRY": "99.0", "IN_SPEC": "F", "ENTERED_BY": "5300"},
        ]).to_sql("RESULT", eng, index=False, if_exists="replace")
        eng.dispose()
        user = get_user_model().objects.create_user(username="sem_t", password="x")
        self.ds = DataSource.objects.create(
            name="egpc", source_type="sqlite", database_name=self.path,
            options={"path": self.path}, owner=user)
        self.spec = {
            "datasource": "egpc",
            "notes": "Use SAMPLE for the register.",
            "tables": {
                "SAMPLE": {"description": "sample register", "synonyms": ["sample"],
                           "columns": {
                               "PRODUCT": {"description": "product", "synonyms": ["product", "grade"]},
                               "IN_SPEC": {"encoding": "T=in-spec, F=off-spec"}}},
                "RESULT": {"columns": {
                    "NUMERIC_ENTRY": {"needs_cast": True},
                    "ENTERED_BY": {"synonyms": ["operator"]}}}},
            "joins": [{"left": "RESULT", "right": "SAMPLE",
                       "on": [["SAMPLE_NUMBER", "SAMPLE_NUMBER"]]}],
            "metrics": [{"key": "samples_by_product", "name": "Samples by product",
                         "synonyms": ["samples by product", "count samples per product"],
                         "sql": "SELECT PRODUCT AS product, COUNT(*) AS n FROM SAMPLE "
                                "GROUP BY PRODUCT ORDER BY n DESC"}]}

    def test_apply_and_load_layer(self):
        counts = apply_spec(self.ds, self.spec, verify=True)
        self.assertEqual(counts["tables"], 2)
        self.assertEqual(counts["metrics"], 1)
        layer = get_semantic_layer(self.ds)
        self.assertTrue(layer.is_present())
        self.assertEqual(layer.table_names(), {"SAMPLE", "RESULT"})
        self.assertEqual(len(layer.joins), 1)
        self.assertEqual(layer.notes, "Use SAMPLE for the register.")

    def test_verify_spec_drift_raises(self):
        bad = dict(self.spec)
        bad["tables"] = {"SAMPLE": {"columns": {"NONEXISTENT": {}}}}
        self.assertTrue(verify_spec(self.ds, bad))  # returns errors
        with self.assertRaises(SpecError):
            apply_spec(self.ds, bad, verify=True)

    def test_build_schema_uses_layer_notes(self):
        from texttosql.schema import build_schema
        apply_spec(self.ds, self.spec, verify=True)
        layer = get_semantic_layer(self.ds)
        ctx = build_schema(self.ds, question="samples by product", layer=layer)
        self.assertIn("SAMPLE", ctx.table_names())
        rendered = ctx.render()
        self.assertIn("T=in-spec, F=off-spec", rendered)        # encoding surfaced
        self.assertIn("Certified joins", rendered)              # join guidance surfaced
        self.assertIn("TRY_CONVERT", rendered)                  # needs_cast surfaced

    def test_certified_metric_fast_path(self):
        from texttosql.runner import run_text_to_sql
        apply_spec(self.ds, self.spec, verify=True)
        out = run_text_to_sql(self.ds, "samples by product")
        self.assertTrue(out["understood"])
        self.assertTrue(out["path"].startswith("certified_metric:samples_by_product"))
        self.assertEqual(out["attempts"], 0)
        self.assertTrue(out["row_count"] >= 1)

    def test_generated_path_validates_against_layer(self):
        from texttosql import runner
        apply_spec(self.ds, self.spec, verify=True)
        with mock.patch.object(runner, "generate_sql", return_value="SELECT PRODUCT FROM SAMPLE"):
            out = runner.run_text_to_sql(self.ds, "list the distinct grades we handle")
        self.assertTrue(out["understood"])
        self.assertEqual(out["path"], "generated")
        self.assertTrue(out["row_count"] >= 1)

    def test_generated_path_rejects_hallucinated_table(self):
        from texttosql import runner
        apply_spec(self.ds, self.spec, verify=True)
        with mock.patch.object(runner, "generate_sql", return_value="SELECT * FROM WIDGETS"):
            out = runner.run_text_to_sql(self.ds, "anything from widgets", max_attempts=2)
        self.assertFalse(out["understood"])
        self.assertIn("SAMPLE", out["available_tables"])

    def test_ask_smoke_command_certified_path(self):
        import json
        from io import StringIO
        from django.core.management import call_command
        apply_spec(self.ds, self.spec, verify=True)
        qf = os.path.join(self.dir, "q.jsonl")
        with open(qf, "w", encoding="utf-8") as f:
            f.write(json.dumps({"question": "samples by product",
                                "expect_keywords": ["DIESEL"]}) + "\n")
        out = StringIO()
        call_command("ask_smoke", datasource=str(self.ds.id), file=qf, stdout=out, stderr=out)
        s = out.getvalue()
        self.assertIn("PASS", s)
        self.assertIn("certified_metric:samples_by_product", s)
