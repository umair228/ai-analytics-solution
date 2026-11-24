"""Named, shareable saved views — and the hole a sharing feature likes to hide in.

The Data Explorer kept its state in **one browser's localStorage**, keyed by dataset,
unnamed. The QA manager's Monday-morning view — the filters, the grouping, the chart, the
date window she had tuned over months — existed only on the laptop she happened to build it
on. She could not name it, could not hand it to whoever covered for her, and clearing the
cache destroyed it.

Giving it a name and a share button is the easy part. The part worth testing is that the
share button is **not a key to data**.

A saved view is a *configuration*: a chart type, a group-by, a set of filters. Its name,
though, is content — *"Crude Distillation — off-spec batches"* tells you a dataset exists and
what is in it. So a view is visible only when the user may see **both the view and the
dataset behind it**. Sharing can never widen access to data; if it could, this feature would
be a hole punched straight through URS-032, and it would look like a feature.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounts.models import Lab, Organization, Site
from connections.models import DataSource
from core.access import Visibility
from core.models import AuditLog
from datasets.models import Dataset, SavedView
from querybuilder.models import QueryDefinition

User = get_user_model()

CONFIG = {"chartTypes": ["bar"], "xField": "product", "yField": "oos_rate",
          "agg": "avg", "filters": [{"col": "conforms", "op": "eq", "val": "N"}],
          "dateRange": "90d", "topN": 20}


class SavedViewBase(APITestCase):
    def setUp(self):
        org = Organization.objects.create(name="EGPC")
        site = Site.objects.create(organization=org, name="Cairo")
        self.crude = Lab.objects.create(site=site, name="Crude Distillation")
        self.lubes = Lab.objects.create(site=site, name="Lubricants")

        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.nadia = User.objects.create_user("nadia", password="x", role=User.Role.ANALYST)
        self.omar = User.objects.create_user("omar", password="x", role=User.Role.ANALYST)
        self.nadia.labs.add(self.crude)
        self.omar.labs.add(self.lubes)

        ds = DataSource.objects.create(name="EGPC LIMS", owner=self.admin,
                                       source_type="mssql", visibility=Visibility.ORG)
        q = QueryDefinition.objects.create(name="q", datasource=ds, owner=self.admin,
                                           mode=QueryDefinition.Mode.RAW, raw_sql="SELECT 1",
                                           visibility=Visibility.ORG)

        # A dataset only Crude Distillation may read.
        self.crude_ds = Dataset.objects.create(
            name="Crude — off-spec batches", query=q, owner=self.admin,
            lab=self.crude, visibility=Visibility.LAB)
        # And one everyone may read.
        self.org_ds = Dataset.objects.create(
            name="Refinery overview", query=q, owner=self.admin,
            visibility=Visibility.ORG)

    def _view(self, user, dataset, name, visibility=Visibility.PRIVATE, lab=None):
        return SavedView.objects.create(
            name=name, dataset=dataset, owner=user, config=CONFIG,
            visibility=visibility, lab=lab)

    def _list(self, user):
        self.client.force_authenticate(user)
        body = self.client.get("/api/saved-views/").json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        return {r["name"] for r in rows}


class SharingAViewMustNotWidenAccessToDataTests(SavedViewBase):
    """The whole design. Everything else here is CRUD."""

    def test_an_ORG_shared_view_of_a_LAB_dataset_is_INVISIBLE_to_the_other_lab(self):
        """Nadia publishes her Crude view to the whole organisation. Omar is in Lubricants
        and cannot read the Crude dataset. He must not see the view — not because the config
        is secret, but because the NAME is content: "Crude — off-spec batches" tells him the
        dataset exists and what is in it."""
        self._view(self.nadia, self.crude_ds, "Crude — off-spec batches",
                   visibility=Visibility.ORG)

        self.assertNotIn("Crude — off-spec batches", self._list(self.omar))
        self.assertIn("Crude — off-spec batches", self._list(self.nadia))

    def test_opening_it_directly_is_refused_too(self):
        """Hiding it from the list is not enough if the id still works."""
        view = self._view(self.nadia, self.crude_ds, "Crude view", visibility=Visibility.ORG)

        self.client.force_authenticate(self.omar)
        self.assertEqual(self.client.get(f"/api/saved-views/{view.id}/").status_code, 404)

    def test_a_view_of_an_ORG_dataset_shared_to_ORG_IS_visible_to_everyone(self):
        """The control must not be so blunt that sharing never works."""
        self._view(self.nadia, self.org_ds, "Refinery overview — my cut",
                   visibility=Visibility.ORG)
        self.assertIn("Refinery overview — my cut", self._list(self.omar))

    def test_you_cannot_SAVE_a_view_of_a_dataset_you_cannot_see(self):
        """Otherwise the create endpoint confirms a dataset id exists and lets you title it."""
        self.client.force_authenticate(self.omar)
        r = self.client.post("/api/saved-views/", {
            "name": "Sneaky", "dataset": self.crude_ds.id, "config": CONFIG,
        }, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertIn("not a dataset you can see", str(r.json()).lower())

    def test_a_LAB_shared_view_reaches_the_lab_and_stops_there(self):
        colleague = User.objects.create_user("hala", password="x", role=User.Role.ANALYST)
        colleague.labs.add(self.crude)

        self._view(self.nadia, self.crude_ds, "Crude — daily", visibility=Visibility.LAB,
                   lab=self.crude)

        self.assertIn("Crude — daily", self._list(colleague))
        self.assertNotIn("Crude — daily", self._list(self.omar))

    def test_an_admin_sees_everything(self):
        self._view(self.nadia, self.crude_ds, "Crude view")
        self.assertIn("Crude view", self._list(self.admin))


class TheViewStaysTheThingYouSharedTests(SavedViewBase):
    def test_someone_you_shared_with_cannot_rewrite_it_under_you(self):
        """Being able to OPEN a shared view is not a licence to change it. The entire point
        of naming and sharing it is that it remains the thing that was shared."""
        view = self._view(self.nadia, self.org_ds, "Overview", visibility=Visibility.ORG)

        self.client.force_authenticate(self.omar)
        r = self.client.patch(f"/api/saved-views/{view.id}/",
                              {"name": "Omar's now"}, format="json")
        self.assertEqual(r.status_code, 403)

        view.refresh_from_db()
        self.assertEqual(view.name, "Overview")

    def test_nor_delete_it(self):
        view = self._view(self.nadia, self.org_ds, "Overview", visibility=Visibility.ORG)
        self.client.force_authenticate(self.omar)
        self.assertEqual(self.client.delete(f"/api/saved-views/{view.id}/").status_code, 403)
        self.assertTrue(SavedView.objects.filter(pk=view.pk).exists())

    def test_the_owner_can(self):
        view = self._view(self.nadia, self.org_ds, "Overview")
        self.client.force_authenticate(self.nadia)
        r = self.client.patch(f"/api/saved-views/{view.id}/",
                              {"name": "Overview v2"}, format="json")
        self.assertEqual(r.status_code, 200)


class WideningAShareIsAudited(SavedViewBase):
    """Changing who can see something is a security-relevant act, and it goes through the
    same before/after machinery as any other controlled record (URS-014)."""

    def test_going_from_private_to_org_shows_up_in_the_trail_as_exactly_that(self):
        view = self._view(self.nadia, self.org_ds, "Overview",
                          visibility=Visibility.PRIVATE)

        self.client.force_authenticate(self.nadia)
        self.client.patch(f"/api/saved-views/{view.id}/",
                          {"visibility": "org"}, format="json")

        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, target_type="SavedView")
        self.assertEqual(entry.detail["changes"]["visibility"],
                         {"from": "private", "to": "org"})
        self.assertEqual(entry.user, self.nadia)

    def test_creating_one_is_audited_with_who_can_see_it(self):
        self.client.force_authenticate(self.nadia)
        self.client.post("/api/saved-views/", {
            "name": "Morning check", "dataset": self.org_ds.id, "config": CONFIG,
            "visibility": "org",
        }, format="json")

        entry = AuditLog.objects.get(action=AuditLog.Action.CREATE, target_type="SavedView")
        self.assertIn("Morning check", entry.summary)
        self.assertIn("Everyone", entry.summary)


class ItActuallySavesTheViewTests(SavedViewBase):
    def test_the_config_round_trips(self):
        self.client.force_authenticate(self.nadia)
        created = self.client.post("/api/saved-views/", {
            "name": "Morning check", "dataset": self.org_ds.id, "config": CONFIG,
        }, format="json").json()

        got = self.client.get(f"/api/saved-views/{created['id']}/").json()
        self.assertEqual(got["config"], CONFIG)
        self.assertEqual(got["dataset_name"], "Refinery overview")
        self.assertTrue(got["is_mine"])

    def test_someone_elses_shared_view_is_not_mine(self):
        view = self._view(self.nadia, self.org_ds, "Hers", visibility=Visibility.ORG)
        self.client.force_authenticate(self.omar)
        got = self.client.get(f"/api/saved-views/{view.id}/").json()
        self.assertFalse(got["is_mine"])
