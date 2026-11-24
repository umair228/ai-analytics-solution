"""Access control, as a matrix — because it was decoration.

`visibility` was read by nothing. PRIVATE and SHARED behaved identically; the seeds marked
every demo object "shared" and every non-admin saw an empty application; the AI agent
offered a laboratory manager zero data sources, so they could not even ask a question.
Eleven roles, and only `admin` could see anything.

So these tests assert the three things that must now be true, and one that must never be:

1. A laboratory user sees their laboratory's work.
2. They do NOT see another laboratory's — the control an ISO 17025 assessor will ask for.
3. Seeing is not editing.
4. The agent sees exactly what its user sees, and not one row more.

Everything routes through ``core.access``, so a rule that holds for the REST list holds
for the object endpoint, the alerts, the widgets and the agent — by construction, not by
four people remembering.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Lab, Organization, Site
from core.access import Visibility, can_edit, can_view
from connections.models import DataSource
from dashboards.models import Dashboard
from datasets.models import Dataset
from querybuilder.models import QueryDefinition

User = get_user_model()


class LabScopeTests(TestCase):
    """Two laboratories in one refinery. The question an assessor actually asks:
    *can Lubricants read Crude Distillation's results?*"""

    def setUp(self):
        org = Organization.objects.create(name="EGPC")
        site = Site.objects.create(organization=org, name="Cairo")
        self.crude = Lab.objects.create(site=site, name="Crude Distillation")
        self.lubes = Lab.objects.create(site=site, name="Lubricants")

        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.crude_chem = User.objects.create_user("nadia", password="x",
                                                   role=User.Role.ANALYST)
        self.lubes_chem = User.objects.create_user("omar", password="x",
                                                   role=User.Role.ANALYST)
        self.crude_chem.labs.add(self.crude)
        self.lubes_chem.labs.add(self.lubes)

        self.ds = DataSource.objects.create(
            name="EGPC LIMS", owner=self.admin, source_type="mssql",
            visibility=Visibility.ORG)          # the connection is org-wide
        self.q = QueryDefinition.objects.create(
            name="q", datasource=self.ds, owner=self.admin,
            mode=QueryDefinition.Mode.RAW, raw_sql="SELECT 1",
            visibility=Visibility.ORG)

        def dataset(name, lab, vis):
            return Dataset.objects.create(name=name, query=self.q, owner=self.admin,
                                          lab=lab, visibility=vis)

        self.crude_ds = dataset("Crude — OOS by product", self.crude, Visibility.LAB)
        self.lubes_ds = dataset("Lubricants — viscosity", self.lubes, Visibility.LAB)
        self.org_ds = dataset("Refinery overview", None, Visibility.ORG)
        self.private_ds = dataset("Admin scratch", None, Visibility.PRIVATE)

    def _names(self, user, url="/api/datasets/"):
        c = APIClient()
        c.force_authenticate(user)
        body = c.get(url).json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        return {r["name"] for r in rows}

    # -- 1. a lab user sees their lab's work ---------------------------------------
    def test_a_lab_user_sees_their_own_labs_datasets(self):
        self.assertIn("Crude — OOS by product", self._names(self.crude_chem))

    def test_a_lab_user_sees_org_wide_datasets(self):
        self.assertIn("Refinery overview", self._names(self.crude_chem))

    # -- 2. and NOT another lab's -------------------------------------------------
    def test_a_lab_user_CANNOT_see_another_labs_datasets(self):
        """The whole point. Lubricants must not read Crude Distillation."""
        names = self._names(self.lubes_chem)
        self.assertNotIn("Crude — OOS by product", names)
        self.assertIn("Lubricants — viscosity", names)

    def test_the_object_endpoint_agrees_with_the_list(self):
        """A list that hides a row while the detail endpoint serves it is not access
        control, it is a filter."""
        c = APIClient()
        c.force_authenticate(self.lubes_chem)
        self.assertEqual(c.get(f"/api/datasets/{self.crude_ds.id}/").status_code, 404)
        self.assertEqual(c.get(f"/api/datasets/{self.lubes_ds.id}/").status_code, 200)

    def test_nobody_sees_someone_elses_private_dataset(self):
        for user in (self.crude_chem, self.lubes_chem):
            self.assertNotIn("Admin scratch", self._names(user))

    def test_a_user_in_no_lab_sees_only_org_wide_content(self):
        """The default a new account lands in. It must fail closed."""
        newcomer = User.objects.create_user("new", password="x", role=User.Role.ANALYST)
        self.assertEqual(self._names(newcomer), {"Refinery overview"})

    def test_an_admin_still_sees_everything(self):
        self.assertEqual(len(self._names(self.admin)), 4)

    # -- 3. seeing is not editing --------------------------------------------------
    def test_seeing_a_lab_dataset_does_not_make_it_yours_to_change(self):
        """If LAB visibility granted write, one careless share would hand a laboratory's
        certified dataset to everyone in it."""
        self.assertTrue(can_view(self.crude_ds, self.crude_chem))
        self.assertFalse(can_edit(self.crude_ds, self.crude_chem))

        c = APIClient()
        c.force_authenticate(self.crude_chem)
        r = c.patch(f"/api/datasets/{self.crude_ds.id}/", {"name": "hijacked"},
                    format="json")
        self.assertIn(r.status_code, (403, 404))
        self.crude_ds.refresh_from_db()
        self.assertEqual(self.crude_ds.name, "Crude — OOS by product")

    def test_the_owner_can_still_edit(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        r = c.patch(f"/api/datasets/{self.crude_ds.id}/", {"name": "renamed"},
                    format="json")
        self.assertEqual(r.status_code, 200)

    # -- 4. the agent sees exactly what its user sees -------------------------------
    def test_the_agent_cannot_reach_further_than_its_user(self):
        """Otherwise "ask the AI" becomes a way to read data the API refuses to hand over
        — the most valuable bypass in the product."""
        from ai.tools import _accessible_datasets, _accessible_datasources

        visible = {d.name for d in _accessible_datasets(self.lubes_chem)}
        self.assertNotIn("Crude — OOS by product", visible)
        self.assertIn("Lubricants — viscosity", visible)

        # And it can now see the LIMS connection at all — before this, it saw none, so a
        # laboratory manager could not ask the system a single question.
        self.assertEqual([d.name for d in _accessible_datasources(self.lubes_chem)],
                         ["EGPC LIMS"])


class TheOldBugStaysFixedTests(TestCase):
    """A regression test for the exact shape the seeds produce."""

    def setUp(self):
        org = Organization.objects.create(name="EGPC")
        site = Site.objects.create(organization=org, name="Cairo")
        lab = Lab.objects.create(site=site, name="Crude Distillation")
        self.admin = User.objects.create_user("admin", password="x", role=User.Role.ADMIN)
        self.mgr = User.objects.create_user("mgr", password="x",
                                            role=User.Role.LAB_MANAGER)
        self.mgr.labs.add(lab)

        ds = DataSource.objects.create(name="EGPC LIMS", owner=self.admin,
                                       source_type="mssql", visibility=Visibility.ORG)
        q = QueryDefinition.objects.create(name="q", datasource=ds, owner=self.admin,
                                           mode=QueryDefinition.Mode.RAW,
                                           raw_sql="SELECT 1", visibility=Visibility.ORG)
        Dataset.objects.create(name="OOS by product", query=q, owner=self.admin,
                               visibility=Visibility.ORG)
        Dashboard.objects.create(name="Lab Overview", owner=self.admin,
                                 visibility=Visibility.ORG)

    def test_a_lab_manager_does_not_log_in_to_an_empty_application(self):
        """Every seed created its content as `owner=admin, visibility="shared"` — and
        nothing read `visibility`, so 26 of the 27 seeded users saw nothing at all. Every
        demo had to be given as `admin`."""
        c = APIClient()
        c.force_authenticate(self.mgr)
        for url in ("/api/datasources/", "/api/queries/", "/api/datasets/",
                    "/api/dashboards/"):
            body = c.get(url).json()
            rows = body.get("results", body) if isinstance(body, dict) else body
            self.assertEqual(len(rows), 1, f"{url} is empty for a laboratory manager")


class TheRuleHasOneDefinitionTests(TestCase):
    """The bug was fourteen copies of an access rule and four copies of an enum. If they
    come back, they will drift again — and the copy that drifts silently is the list."""

    def test_no_app_redeclares_its_own_visibility_enum(self):
        for model in (Dataset, Dashboard, DataSource, QueryDefinition):
            self.assertIs(model.Visibility, Visibility,
                          f"{model.__name__} has its own Visibility again")

    def test_the_dead_shared_value_is_really_gone(self):
        """It has to fail loudly. A string that is not a valid choice still *saves* —
        Django does not validate choices on save — so a lingering `visibility="shared"`
        would silently mean "visible to nobody", which is how this started."""
        self.assertFalse(hasattr(Visibility, "SHARED"))
        self.assertEqual({v.value for v in Visibility}, {"private", "lab", "org"})

    def test_no_app_grows_its_own_private_copy_of_the_rule(self):
        """The rule was copy-pasted into fourteen places. When I first swept them I found
        thirteen — `semantics`, `coa`, `replication` and `analytics` each held another,
        and a dataset visible to your laboratory was listed by /api/datasets/ and 404'd by
        the analytics endpoints. A grep is a poor test, but it is the test that would have
        caught that, so here it is.

        If you are adding a legitimate exception (AnalysisRun is one — see its docstring),
        say so on the same line and this will let it through.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        pattern = re.compile(r"Q\(owner=user\)\s*\|\s*Q\(shared_with=user\)")
        offenders = []
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if ".venv" in parts or "migrations" in parts or path.name.startswith("test"):
                continue
            if path.name == "access.py":
                continue
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    context = "\n".join(path.read_text().splitlines()[max(0, i - 12):i])
                    if "NOT core.access" in context or "NARROWER" in context:
                        continue          # a documented, deliberate exception
                    offenders.append(f"{path.relative_to(root)}:{i}")

        self.assertEqual(offenders, [], "\n  These re-implement the access rule instead of "
                                        "calling core.access:\n    " + "\n    ".join(offenders))
