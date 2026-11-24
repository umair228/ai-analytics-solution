"""Snapshot cache, warm-up and performance budget for boards (P10).

A real SQLite "restored copy" stands in for the reporting source: the cache is
proven by changing the data underneath it, and the warm-up by checking that the
requests the UI makes are then served from the cache.
"""
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from rest_framework.test import APIClient

from connections.models import DataSource
from dashboards.models import Dashboard, Widget
from datasets.explore import run_explore, widget_explore_bodies
from datasets.models import Dataset
from querybuilder.models import QueryDefinition


class BoardCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "copy.sqlite")
        self.sql("CREATE TABLE t (status TEXT, n INTEGER)")
        self.sql("INSERT INTO t VALUES ('open', 2), ('open', 3), ('closed', 5)")
        User = get_user_model()
        self.admin = User.objects.create_user(username="adm", password="x", role="admin")
        self.viewer = User.objects.create_user(username="v", password="x", role="viewer")
        self.source = DataSource.objects.create(
            name="Copy", source_type="sqlite", database_name=self.db,
            options={"path": self.db, "restore_id": "r-1"}, owner=self.admin)
        query = QueryDefinition.objects.create(
            name="Items", datasource=self.source, mode=QueryDefinition.Mode.RAW,
            raw_sql="SELECT status, n FROM t", owner=self.admin)
        self.dataset = Dataset.objects.create(name="Items", query=query, owner=self.admin)
        self.board = Dashboard.objects.create(name="Board", owner=self.admin)
        self.kpi = Widget.objects.create(dashboard=self.board, widget_type="kpi", title="Count",
                                         dataset=self.dataset, config={"rollup": "count"})
        self.bar = Widget.objects.create(dashboard=self.board, widget_type="bar", title="By status",
                                         dataset=self.dataset,
                                         config={"category_field": "status", "value_field": "n",
                                                 "rollup": "sum",
                                                 "filters": [{"column": "n", "type": "compare",
                                                              "op": ">", "value": 0}]})

    def tearDown(self):
        self.tmp.cleanup()
        cache.clear()

    def sql(self, statement):
        con = sqlite3.connect(self.db)
        con.execute(statement)
        con.commit()
        con.close()

    def scalar(self, result):
        return result["value"]          # scalar mode: {"value": ..., "agg": ...}

    def test_the_bodies_mirror_the_dashboard_ui(self):
        self.assertEqual(widget_explore_bodies(self.kpi),
                         [{"mode": "scalar", "y": None, "agg": "count", "filters": []}])
        (bar,) = widget_explore_bodies(self.bar)
        self.assertEqual((bar["mode"], bar["x"], bar["y"], bar["agg"], bar["limit"]),
                         ("aggregate", "status", "n", "sum", 300))
        self.assertEqual(bar["filters"], self.bar.config["filters"])   # the widget's own filters
        multi = Widget(dashboard=self.board, widget_type="line", dataset=self.dataset,
                       config={"category_field": "status", "value_field": "n",
                               "value_fields": ["n", "m"]})
        self.assertEqual(widget_explore_bodies(multi), [])            # the UI aggregates these itself
        self.assertEqual(widget_explore_bodies(Widget(dashboard=self.board, widget_type="studio",
                                                      dataset=self.dataset)), [])

    def test_a_snapshot_result_is_cached_until_the_restore_changes(self):
        body = widget_explore_bodies(self.kpi)[0]
        first = run_explore(self.dataset, body)
        self.assertEqual((first["source"], self.scalar(first)), ("database", 3))
        self.sql("INSERT INTO t VALUES ('open', 1)")                # the copy cannot change...
        second = run_explore(self.dataset, body)
        self.assertEqual((second["source"], self.scalar(second)), ("cache", 3))
        self.source.options = {**self.source.options, "restore_id": "r-2"}   # ...until promoted
        self.source.save()
        self.dataset.refresh_from_db()
        third = run_explore(self.dataset, body)
        self.assertEqual((third["source"], self.scalar(third)), ("database", 4))

    def test_a_live_source_is_never_cached(self):
        self.source.options = {"path": self.db}
        self.source.save()
        self.dataset.refresh_from_db()
        body = widget_explore_bodies(self.kpi)[0]
        self.assertEqual(run_explore(self.dataset, body)["source"], "database")
        self.assertEqual(run_explore(self.dataset, body)["source"], "database")

    def test_the_key_is_the_request(self):
        body = widget_explore_bodies(self.kpi)[0]
        run_explore(self.dataset, body)
        filtered = dict(body, filters=[{"column": "status", "type": "in", "values": ["open"]}])
        result = run_explore(self.dataset, filtered)
        self.assertEqual((result["source"], self.scalar(result)), ("database", 2))

    def test_warm_boards_fills_exactly_what_the_ui_reads(self):
        out = StringIO()
        call_command("warm_boards", datasource=["Copy"], stdout=out)
        self.assertIn("Warmed 1 board(s), 2 call(s), 0 failed", out.getvalue())
        for widget in (self.kpi, self.bar):
            for body in widget_explore_bodies(widget):
                self.assertEqual(run_explore(self.dataset, body)["source"], "cache", widget.title)

    def test_perf_budget_measures_and_gates(self):
        out = StringIO()
        call_command("perf_budget", datasource=["Copy"], runs=2, concurrency=1,
                     budget_ms=60000, stdout=out)
        self.assertIn("All 1 board(s) within 60000 ms.", out.getvalue())
        with self.assertRaisesMessage(CommandError, "over the 0 ms budget"):
            call_command("perf_budget", datasource=["Copy"], runs=1, concurrency=1,
                         budget_ms=0, cold=True, stdout=StringIO())

    def test_the_api_still_checks_access_before_the_cache(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        url = f"/api/datasets/{self.dataset.id}/explore/"
        body = widget_explore_bodies(self.kpi)[0]
        self.assertEqual(client.post(url, body, format="json").json()["source"], "database")
        self.assertEqual(client.post(url, body, format="json").json()["source"], "cache")
        client.force_authenticate(self.viewer)          # private dataset, not shared
        self.assertEqual(client.post(url, body, format="json").status_code, 404)
