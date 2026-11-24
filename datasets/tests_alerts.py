"""Alerts that fire on an ANALYSIS, not just a threshold on a column.

An alert could only ever say "average(sulphur) > 12" — a useful thing to say and a
poor thing to be limited to. None of the questions a laboratory actually monitors
can be written that way:

    "tell me when a Westgard rule fires on QC-101"
    "tell me when Cpk drops below 1.33"
    "tell me when the off-spec rate is projected to breach 2%"

The first test class here is the most important one: the pre-existing threshold path
must behave exactly as before.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from datasets.models import AlertEvent, Dataset, DatasetAlert
from datasets.services import evaluate_alerts


class _Base(TestCase):
    def setUp(self):
        from connections.models import DataSource
        from querybuilder.models import QueryDefinition

        self.user = get_user_model().objects.create_user(username="qc", password="x")
        self.source = DataSource.objects.create(
            name="lims", source_type="sqlite", owner=self.user,
            options={"path": "/tmp/alerts.sqlite"})
        self.query = QueryDefinition.objects.create(
            name="qc runs", datasource=self.source, mode=QueryDefinition.Mode.RAW,
            raw_sql="SELECT qc FROM result", owner=self.user)

    def _dataset(self, columns, rows):
        return Dataset.objects.create(
            name="QC runs", owner=self.user, query=self.query,
            cached_columns=columns,
            cached_rows=[dict(zip(columns, r)) for r in rows],
            row_count=len(rows), total_row_count=len(rows))

    def _alert(self, dataset, **kw):
        defaults = dict(name="A", dataset=dataset, owner=self.user, column="qc",
                        condition="gt", threshold=0.0)
        defaults.update(kw)
        return DatasetAlert.objects.create(**defaults)

    def _events(self, alert):
        return list(AlertEvent.objects.filter(alert=alert))


class ThresholdAlertsAreUnchangedTests(_Base):
    """The regression that matters: the old path must behave exactly as before."""

    def test_threshold_alert_still_fires(self):
        ds = self._dataset(["qc"], [[10], [12], [14]])
        a = self._alert(ds, aggregation="avg", condition="gt", threshold=11.0)

        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0].triggered_value, 12.0)
        # The message format is unchanged, verbatim — anything watching for it still works.
        self.assertEqual(
            ev[0].message,
            "Alert 'A' triggered: avg(qc) > 11.0 (current value: 12)")

    def test_threshold_alert_does_not_fire_below_the_line(self):
        ds = self._dataset(["qc"], [[1], [2], [3]])
        a = self._alert(ds, aggregation="avg", condition="gt", threshold=99.0)
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])

    def test_kind_defaults_to_threshold(self):
        ds = self._dataset(["qc"], [[1]])
        self.assertEqual(self._alert(ds).kind, DatasetAlert.Kind.THRESHOLD)


class SpcAlertTests(_Base):
    """The flagship: 'tell me when a Westgard rule fires on QC-101'."""

    def _qc_runs(self, shifted=True):
        rng = np.random.default_rng(42)
        stable = list(100 + rng.normal(0, 1.0, 10))
        tail = list(104.4 + rng.normal(0, 1.0, 8)) if shifted \
            else list(100 + rng.normal(0, 1.0, 8))
        return [[round(v, 3)] for v in stable + tail]

    def _spc_alert(self, ds):
        return self._alert(
            ds, name="QC-101 Westgard", kind=DatasetAlert.Kind.SPC,
            condition="gt", threshold=0.0,     # any rejection at all
            config={"test": "levey_jennings", "mean": 100.0, "sd": 2.0,
                    "rules": "westgard"})

    def test_a_westgard_violation_fires_the_alert(self):
        ds = self._dataset(["qc"], self._qc_runs(shifted=True))
        a = self._spc_alert(ds)

        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)
        self.assertGreater(ev[0].triggered_value, 0)      # rejection count

        # The message must name the rule and the error type. "Out of control" is a
        # verdict; "2₂ₛ — systematic error" is what sends the analyst to the right place.
        self.assertIn("2₂ₛ", ev[0].message)
        self.assertIn("SYSTEMATIC", ev[0].message)
        self.assertIn("systematic", ev[0].detail["error_types"])
        self.assertTrue(ev[0].detail["established_limits"])
        self.assertTrue(ev[0].detail["violations"])

    def test_an_in_control_run_does_not_fire(self):
        ds = self._dataset(["qc"], self._qc_runs(shifted=False))
        a = self._spc_alert(ds)
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])

    def test_a_1_2s_warning_alone_does_not_fire(self):
        """1₂ₛ is a warning, not a rejection. An alert that fires on it would page
        someone roughly one run in twenty, forever — which is how a monitoring
        scheme gets switched off."""
        ds = self._dataset(["qc"], [[100], [100], [100], [100], [100], [104.5]])
        a = self._spc_alert(ds)
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])

    def test_the_estimated_limits_caveat_reaches_the_event(self):
        """If the limits were fitted to the data they under-report a shift, so an
        alert built on them is worth less than it looks. The event must say so."""
        ds = self._dataset(["qc"], self._qc_runs(shifted=True))
        a = self._alert(ds, kind=DatasetAlert.Kind.SPC, condition="gt", threshold=0.0,
                        config={"test": "levey_jennings", "rules": "westgard"})
        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0].detail["established_limits"])
        self.assertIn("CHARACTERISATION", ev[0].detail["warning"])

    def test_a_drift_alert_can_use_ewma(self):
        rng = np.random.default_rng(17)
        rows = [[round(v, 3)] for v in
                list(100 + rng.normal(0, 1, 30)) + list(101 + rng.normal(0, 1, 25))]
        ds = self._dataset(["qc"], rows)
        a = self._alert(ds, kind=DatasetAlert.Kind.SPC, condition="gt", threshold=0.0,
                        config={"test": "control_chart_ewma", "mean": 100.0, "sd": 1.0})
        evaluate_alerts(ds)
        self.assertEqual(len(self._events(a)), 1)


class CapabilityAlertTests(_Base):
    def test_cpk_below_target_fires(self):
        rng = np.random.default_rng(1)
        rows = [[round(v, 4)] for v in rng.normal(10, 1.0, 60)]   # wide vs the spec
        ds = self._dataset(["qc"], rows)
        a = self._alert(ds, name="Cpk", kind=DatasetAlert.Kind.CAPABILITY,
                        condition="lt", threshold=1.33,
                        config={"lsl": 8.0, "usl": 12.0})

        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)
        self.assertLess(ev[0].triggered_value, 1.33)
        self.assertIn("Cpk", ev[0].message)
        self.assertIn("NOT capable", ev[0].message)

    def test_a_capable_process_does_not_fire(self):
        rng = np.random.default_rng(1)
        rows = [[round(v, 4)] for v in rng.normal(10, 0.15, 60)]  # tight
        ds = self._dataset(["qc"], rows)
        a = self._alert(ds, kind=DatasetAlert.Kind.CAPABILITY, condition="lt",
                        threshold=1.33, config={"lsl": 8.0, "usl": 12.0})
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])

    def test_missing_spec_limits_do_not_fire_a_false_alert(self):
        """No spec limit means no capability. Firing on a missing number is worse
        than not firing — it teaches people the alert is noise."""
        ds = self._dataset(["qc"], [[1], [2], [3], [4], [5]])
        a = self._alert(ds, kind=DatasetAlert.Kind.CAPABILITY, condition="lt",
                        threshold=1.33, config={})
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])
        a.refresh_from_db()
        self.assertIsNotNone(a.last_checked_at)      # it WAS checked…
        self.assertIsNone(a.last_triggered_at)       # …it just could not conclude


class AnomalyAlertTests(_Base):
    def test_anomalies_fire_the_alert(self):
        rows = [[10.0]] * 30 + [[95.0]]
        ds = self._dataset(["qc"], rows)
        a = self._alert(ds, name="Anomalies", kind=DatasetAlert.Kind.ANOMALY,
                        condition="gt", threshold=0.0,
                        config={"scope": "univariate", "method": "zscore"})
        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)
        self.assertGreaterEqual(ev[0].triggered_value, 1)
        self.assertIn("anomal", ev[0].message)


class ForecastAlertTests(_Base):
    def test_a_projected_breach_fires_before_it_happens(self):
        """The whole point: the current value is still fine. The forecast is not."""
        rows = [[float(1.0 + 0.12 * i)] for i in range(20)]   # 1.0 → 3.3, rising
        ds = self._dataset(["qc"], rows)
        a = self._alert(ds, name="Off-spec rate forecast",
                        kind=DatasetAlert.Kind.FORECAST,
                        condition="gt", threshold=4.5, config={"periods": 12})

        current = rows[-1][0]
        self.assertLess(current, 4.5)          # not breached yet…

        evaluate_alerts(ds)
        ev = self._events(a)
        self.assertEqual(len(ev), 1)           # …but it is going to be
        self.assertGreater(ev[0].triggered_value, 4.5)
        self.assertIn("projected", ev[0].message)

    def test_a_flat_series_does_not_fire(self):
        ds = self._dataset(["qc"], [[1.0]] * 20)
        a = self._alert(ds, kind=DatasetAlert.Kind.FORECAST, condition="gt",
                        threshold=4.5, config={"periods": 12})
        evaluate_alerts(ds)
        self.assertEqual(self._events(a), [])


class CooldownTests(_Base):
    def test_cooldown_suppresses_a_repeat_firing(self):
        """An unresolved violation re-firing every refresh is how a monitoring
        scheme gets ignored."""
        ds = self._dataset(["qc"], [[10], [12], [14]])
        a = self._alert(ds, aggregation="avg", condition="gt", threshold=11.0,
                        cooldown_minutes=60)

        evaluate_alerts(ds)
        self.assertEqual(len(self._events(a)), 1)

        evaluate_alerts(ds)                       # still breached, still hot
        self.assertEqual(len(self._events(a)), 1)

        a.refresh_from_db()
        a.last_triggered_at = timezone.now() - timedelta(minutes=61)
        a.save(update_fields=["last_triggered_at"])
        evaluate_alerts(ds)                       # cooldown elapsed
        self.assertEqual(len(self._events(a)), 2)

    def test_zero_cooldown_keeps_the_old_every_check_behaviour(self):
        ds = self._dataset(["qc"], [[10], [12], [14]])
        a = self._alert(ds, aggregation="avg", condition="gt", threshold=11.0)
        evaluate_alerts(ds)
        evaluate_alerts(ds)
        self.assertEqual(len(self._events(a)), 2)


class RobustnessTests(_Base):
    def test_one_broken_rule_does_not_stop_the_others(self):
        ds = self._dataset(["qc"], [[10], [12], [14]])
        broken = self._alert(ds, name="broken", kind=DatasetAlert.Kind.SPC,
                             condition="gt", threshold=0.0,
                             config={"test": "no_such_test"})
        good = self._alert(ds, name="good", aggregation="avg",
                           condition="gt", threshold=11.0)

        evaluate_alerts(ds)                       # must not raise

        self.assertEqual(self._events(broken), [])
        self.assertEqual(len(self._events(good)), 1)
        broken.refresh_from_db()
        self.assertIsNotNone(broken.last_checked_at)
