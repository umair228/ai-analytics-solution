"""Freshness must be derived, not asserted.

`texttosql.runner` used to stamp every answer `freshness: "live"` as a literal, and
`data_as_of` — reserved in the export provenance footer — was never set by anything.
An answer off a three-day-stale replica was indistinguishable, in its own
provenance, from a genuinely live one.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from connections.models import DataSource
from replication.freshness import describe
from replication.models import ReplicationState


class FreshnessTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="f", password="x")
        self.source = self._ds("EGPC_DEV", "mssql")
        self.replica = self._ds("EGPC_REPLICA", "postgres")

    def _ds(self, name, source_type):
        return DataSource.objects.create(
            name=name, source_type=source_type, database_name=f"/tmp/{name}",
            options={"path": f"/tmp/{name}"}, owner=self.user)

    def _state(self, table, synced_ago=None, cadence="hourly", error=""):
        at = None if synced_ago is None else timezone.now() - synced_ago
        return ReplicationState.objects.create(
            source=self.source, target=self.replica, table_name=table,
            cadence=cadence, last_full_at=at, last_incremental_at=at, last_error=error)

    def test_a_source_with_nothing_replicating_into_it_is_live(self):
        f = describe(self.source)
        self.assertEqual(f.kind, "live")
        self.assertIsNone(f.data_as_of)
        self.assertEqual(f.warnings(), [])

    def test_a_fresh_replica_reports_replica_and_a_real_data_as_of(self):
        self._state("SAMPLE", timedelta(minutes=10))
        self._state("RESULT", timedelta(minutes=5))

        f = describe(self.replica)
        self.assertEqual(f.kind, "replica")
        self.assertIsNotNone(f.data_as_of)
        self.assertEqual(f.warnings(), [])

    def test_the_slowest_table_bounds_the_answer(self):
        """A query joining SAMPLE and RESULT is only as fresh as the staler of the
        two, so data_as_of is the MINIMUM — never the maximum, which would flatter
        the answer."""
        self._state("SAMPLE", timedelta(minutes=50))
        self._state("RESULT", timedelta(minutes=5))

        as_of = describe(self.replica).data_as_of
        oldest = ReplicationState.objects.get(table_name="SAMPLE").last_synced_at
        self.assertEqual(as_of, oldest.isoformat())

    def test_an_overdue_table_makes_the_whole_answer_stale_and_warns(self):
        self._state("SAMPLE", timedelta(minutes=5))
        self._state("RESULT", timedelta(hours=9))          # hourly cadence → overdue

        f = describe(self.replica)
        self.assertEqual(f.kind, "stale")
        self.assertEqual(f.stale_tables, ["RESULT"])
        self.assertIn("RESULT", f.warnings()[0])

    def test_a_standing_replication_error_is_stale_even_if_recent(self):
        self._state("SAMPLE", timedelta(minutes=1), error="extract failed: login timeout")
        self.assertEqual(describe(self.replica).kind, "stale")

    def test_a_replica_that_never_synced_is_stale_not_live(self):
        self._state("SAMPLE", None)
        f = describe(self.replica)
        self.assertEqual(f.kind, "stale")
        self.assertIsNone(f.data_as_of)

    def test_provenance_carries_data_as_of(self):
        self._state("SAMPLE", timedelta(minutes=10))
        prov = describe(self.replica).as_provenance()
        self.assertEqual(prov["freshness"], "replica")
        self.assertIsNotNone(prov["data_as_of"])
        self.assertIn("replica", prov["freshness_detail"].lower())
