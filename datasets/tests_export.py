"""Audited dataset export (GET /api/datasets/<id>/export/) and the branded
CSV / XLSX / PDF renderers behind it."""
import datetime
import io
import os
import sqlite3
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from openpyxl import load_workbook
from pypdf import PdfReader
from rest_framework.test import APIClient

from connections.models import DataSource
from core.models import AuditLog
from core.testing import REPORTING_DEFAULTS
from core.report_render import (
    ReportMeta, export_filename, format_stamp, render, render_csv,
)
from datasets.models import Dataset
from datasets.services import refresh_dataset
from querybuilder.models import QueryDefinition

User = get_user_model()

ROWS = 120  # > the 50-row preview cap the tests force, < every export ceiling
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def make_items_db(path, n=ROWS):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER, project TEXT, status TEXT, "
                 "qty REAL, updated_at TEXT)")
    conn.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?)", [
        (i, f"P{i % 7}", "closed" if i % 3 == 0 else "open", i * 1.5,
         "2026-11-22T21:00:00+00:00")
        for i in range(n)])
    conn.commit()
    conn.close()


def sheet_layout(content):
    """(worksheet, header row number, {label: value} of the header block)."""
    ws = load_workbook(io.BytesIO(content)).active
    header_row = int(ws.freeze_panes[1:]) - 1
    block = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(1, header_row)}
    return ws, header_row, block


@override_settings(**REPORTING_DEFAULTS)
class ExportEndpointTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        make_items_db(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        os.unlink(cls.db_path)

    def setUp(self):
        self.owner = User.objects.create_user("exp-owner", password="x", role="analyst")
        self.viewer = User.objects.create_user("exp-viewer", password="x", role="viewer",
                                               first_name="Vera", last_name="Viewer")
        self.stranger = User.objects.create_user("exp-stranger", password="x", role="viewer")
        self.source = DataSource.objects.create(
            name="exp-src", source_type="sqlite", owner=self.owner,
            options={"path": self.db_path})
        self.query = QueryDefinition.objects.create(
            name="items", datasource=self.source, mode=QueryDefinition.Mode.RAW,
            owner=self.owner,
            raw_sql=("SELECT id, project, status, qty, updated_at AS data_as_of FROM items "
                     "WHERE (:status = '' OR status = :status) ORDER BY id"),
            parameters=[{"name": "status", "type": "text", "default": ""}])
        self.dataset = Dataset.objects.create(
            name="ENG-01 Engineering progress", query=self.query, owner=self.owner,
            description="Progress by project.")
        self.dataset.shared_with.add(self.viewer)
        with override_settings(DSE_QUERY_MAX_ROWS=50):
            refresh_dataset(self.dataset)  # the cached preview holds only 50 rows
        self.dataset.refresh_from_db()
        self.assertEqual(len(self.dataset.cached_rows), 50)
        self.client = APIClient()

    def export(self, user=None, dataset=None, **params):
        if user is not None:
            self.client.force_authenticate(user)
        return self.client.get(f"/api/datasets/{(dataset or self.dataset).pk}/export/", params)

    def exports(self):
        return AuditLog.objects.filter(action="EXPORT")

    # ---- data-source freshness hook -----------------------------------------

    def _no_as_of_column(self):
        self.dataset.query.raw_sql = "SELECT id, project, status, qty FROM items"
        self.dataset.query.save()
        refresh_dataset(self.dataset)

    def test_freshness_sql_states_the_source_time_as_data_as_of(self):
        self._no_as_of_column()
        ds = self.dataset.query.datasource
        ds.options = {**(ds.options or {}),
                      "freshness_sql": "SELECT MAX(updated_at) FROM items"}
        ds.save()
        resp = self.export(self.viewer, format="xlsx")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        _, _, block = sheet_layout(resp.content)
        self.assertIn("2026-11-23 00:00", block["Data as of"])  # source time, Riyadh
        self.assertIn("Refreshed", block)

    def test_failing_freshness_sql_falls_back_to_the_refresh_time(self):
        self._no_as_of_column()
        ds = self.dataset.query.datasource
        ds.options = {**(ds.options or {}), "freshness_sql": "SELECT nope FROM missing_table"}
        ds.save()
        resp = self.export(self.viewer, format="xlsx")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        _, _, block = sheet_layout(resp.content)
        self.assertIn(f"({settings.DIS_REPORT_TZ})", block["Data as of"])
        self.assertNotIn("Refreshed", block)

    # ---- formats -----------------------------------------------------------

    def test_xlsx_carries_full_result_header_and_audit(self):
        resp = self.export(self.viewer, format="xlsx")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp["Content-Type"], XLSX)
        self.assertRegex(resp["Content-Disposition"],
                         r'^attachment; filename="eng-01-engineering-progress-\d{8}\.xlsx"$')
        ws, header_row, block = sheet_layout(resp.content)
        self.assertEqual([c.value for c in ws[header_row]],
                         ["id", "project", "status", "qty", "data_as_of"])
        self.assertEqual(ws.max_row - header_row, ROWS)  # full result, not the 50-row cache
        self.assertEqual(ws.auto_filter.ref, f"A{header_row}:E{header_row + ROWS}")
        self.assertEqual(ws["A1"].value, "Data Science Engine")  # no brand set: lab wording
        self.assertEqual(ws["B1"].value, "ENG-01 Engineering progress")
        # the source's own time is "Data as of"; the cache time is "Refreshed"
        self.assertIn("2026-11-23 00:00", block["Data as of"])  # 21:00Z in Riyadh
        self.assertIn(f"({settings.DIS_REPORT_TZ})", block["Refreshed"])
        self.assertEqual(block["Generated by"], "Vera Viewer (exp-viewer)")
        self.assertEqual(block["Rows"], "120")

        row = self.exports().get()
        self.assertEqual((row.user, row.target_type, row.target_id),
                         (self.viewer, "Dataset", str(self.dataset.pk)))
        self.assertEqual(row.detail["format"], "xlsx")
        self.assertEqual(row.detail["row_count"], ROWS)
        self.assertEqual(row.detail["params"], {"status": ""})
        self.assertTrue(row.detail["filename"].endswith(".xlsx"))
        self.assertEqual(row.detail["data_as_of"], "2026-11-22T21:00:00+00:00")

    def test_csv_is_utf8_bom_with_header_block(self):
        resp = self.export(self.owner, format="csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(resp.content.startswith(b"\xef\xbb\xbf"))
        lines = resp.content.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0], "# Data Science Engine,ENG-01 Engineering progress")
        self.assertTrue(any(line.startswith("# Data as of,") for line in lines))
        header_at = lines.index("id,project,status,qty,data_as_of")
        self.assertEqual(lines[header_at - 1], "")
        self.assertEqual(len(lines) - header_at - 1, ROWS)
        self.assertRegex(resp["Content-Disposition"], r'\.csv"$')

    @override_settings(DIS_BRAND_PRODUCT_NAME="Acme Reporting",
                       DIS_BRAND_FOOTER="Internal use only")
    def test_pdf_has_brand_header_footer_and_page_numbers(self):
        resp = self.export(self.viewer, format="pdf")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertRegex(resp["Content-Disposition"], r'-\d{8}\.pdf"$')
        pages = PdfReader(io.BytesIO(resp.content)).pages
        self.assertGreater(len(pages), 1)
        first, last = pages[0].extract_text(), pages[-1].extract_text()
        for expected in ("Acme Reporting", "ENG-01 Engineering progress", "Data as of",
                         "Internal use only", "Generated by Vera Viewer (exp-viewer)",
                         f"Page 1 of {len(pages)}"):
            self.assertIn(expected, first)
        self.assertIn(f"Page {len(pages)} of {len(pages)}", last)
        self.assertIn("data_as_of", pages[1].extract_text())  # header row repeated
        self.assertEqual(self.exports().get().detail["format"], "pdf")

    def test_params_reach_the_query_and_the_audit(self):
        resp = self.export(self.viewer, format="xlsx", params='{"status": "closed"}')
        self.assertEqual(resp.status_code, 200)
        ws, header_row, block = sheet_layout(resp.content)
        self.assertEqual(ws.max_row - header_row, ROWS // 3)  # every third id is closed
        self.assertEqual(block["Parameters"], "status=closed")
        self.assertEqual(self.exports().get().detail["params"], {"status": "closed"})

    def test_filters_apply_server_side_and_unknown_columns_are_reported(self):
        filters = ('[{"column": "project", "type": "dropdown", "value": "P1"},'
                   ' {"column": "nope", "type": "dropdown", "value": "x"}]')
        resp = self.export(self.viewer, format="xlsx", filters=filters)
        self.assertEqual(resp.status_code, 200)
        ws, header_row, block = sheet_layout(resp.content)
        self.assertEqual(ws.max_row - header_row, len([i for i in range(ROWS) if i % 7 == 1]))
        self.assertEqual(block["Filters not applied"], "nope")
        detail = self.exports().get().detail
        self.assertEqual(detail["skipped_filters"], ["nope"])
        self.assertEqual(detail["filters"][0]["column"], "project")

    def test_null_params_and_filters_mean_not_given(self):
        resp = self.export(self.viewer, format="csv", params="null", filters="null")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.exports().get().detail["row_count"], ROWS)

    def test_filters_heal_a_stale_column_list_like_the_data_endpoint(self):
        # The query gained an output column after the last refresh: the filter
        # on it is not dropped — the cached columns are refreshed first.
        self.query.raw_sql = self.query.raw_sql.replace(
            "SELECT id,", "SELECT id, project AS proj_code,")
        self.query.save()
        resp = self.export(self.viewer, format="xlsx",
                           filters='[{"column": "proj_code", "type": "dropdown", "value": "P2"}]')
        self.assertEqual(resp.status_code, 200)
        ws, header_row, block = sheet_layout(resp.content)
        self.assertEqual(ws.max_row - header_row, len([i for i in range(ROWS) if i % 7 == 2]))
        self.assertNotIn("Filters not applied", block)

        # The cache claims a column the query no longer outputs: the database
        # rejects the filter, the columns are refreshed and it is reported.
        self.dataset.refresh_from_db()
        self.dataset.cached_columns = [*self.dataset.cached_columns, "legacy_col"]
        self.dataset.save()
        resp = self.export(self.viewer, format="xlsx",
                           filters='[{"column": "legacy_col", "type": "dropdown", "value": "x"}]')
        self.assertEqual(resp.status_code, 200)
        _, _, block = sheet_layout(resp.content)
        self.assertEqual(block["Filters not applied"], "legacy_col")

    def test_builder_dataset_exports_beyond_the_preview_cap(self):
        spec = {"base_table": {"schema": None, "name": "items"},
                "columns": [{"table": "items", "column": "id"},
                            {"table": "items", "column": "status"}]}
        query = QueryDefinition.objects.create(
            name="spec", datasource=self.source, mode=QueryDefinition.Mode.BUILDER,
            owner=self.owner, spec=spec)
        dataset = Dataset.objects.create(name="Spec items", query=query, owner=self.owner)
        with override_settings(DSE_QUERY_MAX_ROWS=50):
            resp = self.export(self.owner, dataset=dataset, format="csv")
        self.assertEqual(resp.status_code, 200)
        lines = resp.content.decode("utf-8-sig").splitlines()
        self.assertEqual(len(lines) - lines.index("id,status") - 1, ROWS)
        # ...but a spec's own LIMIT is part of its meaning and is kept.
        query.spec = {**spec, "limit": 10}
        query.save()
        resp = self.export(self.owner, dataset=dataset, format="csv")
        lines = resp.content.decode("utf-8-sig").splitlines()
        self.assertEqual(len(lines) - lines.index("id,status") - 1, 10)

    # ---- access ------------------------------------------------------------

    def test_access_is_the_same_as_viewing_the_dataset(self):
        self.assertEqual(self.export(self.stranger, format="csv").status_code, 404)
        self.assertEqual(self.client.get(  # the stranger cannot view it either
            f"/api/datasets/{self.dataset.pk}/").status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.export(format="csv").status_code, 401)
        admin = User.objects.create_superuser("exp-admin", "a@x.co", "x")
        self.assertEqual(self.export(admin, format="csv").status_code, 200)
        self.assertEqual(self.exports().count(), 1)  # only the allowed one

    # ---- limits and failures ------------------------------------------------

    def test_row_ceiling_returns_413_without_file_or_audit(self):
        with override_settings(DIS_EXPORT_MAX_ROWS=100):
            resp = self.export(self.viewer, format="xlsx")
        self.assertEqual(resp.status_code, 413)
        self.assertNotIn("Content-Disposition", resp)
        self.assertIn("more than 100 rows", resp.json()["detail"])
        self.assertEqual(resp.json()["row_limit"], 100)

        with override_settings(DIS_EXPORT_PDF_MAX_ROWS=10):
            resp = self.export(self.viewer, format="pdf")
            self.assertEqual(resp.status_code, 413)
            self.assertIn("PDF limit", resp.json()["detail"])
            # the PDF cap does not apply to spreadsheet formats
            self.assertEqual(self.export(self.viewer, format="csv").status_code, 200)
        self.assertEqual(self.exports().count(), 1)

    def test_audit_failure_means_500_and_no_file(self):
        with mock.patch.object(AuditLog, "save", side_effect=DatabaseError("disk full")), \
                self.assertLogs("core.audit", level="ERROR") as logs:
            resp = self.export(self.viewer, format="xlsx")
        self.assertIn("disk full", logs.output[0])
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json(), {"detail": "Export not audited; nothing was exported."})
        self.assertNotIn("Content-Disposition", resp)
        self.assertTrue(resp["Content-Type"].startswith("application/json"))
        self.assertEqual(self.exports().count(), 0)

    def test_bad_requests_are_400(self):
        self.assertEqual(self.export(self.viewer, format="docx").status_code, 400)
        self.assertEqual(self.export(self.viewer, format="csv", params="not json").status_code, 400)
        self.assertEqual(self.export(self.viewer, format="csv", params="[1]").status_code, 400)
        self.assertEqual(self.export(self.viewer, format="csv", filters='{"a": 1}').status_code, 400)
        self.assertEqual(self.exports().count(), 0)

    def test_default_format_is_xlsx_and_head_does_not_export(self):
        resp = self.export(self.viewer)
        self.assertEqual(resp["Content-Type"], XLSX)
        self.assertEqual(self.client.head(
            f"/api/datasets/{self.dataset.pk}/export/?format=csv").status_code, 405)
        self.assertEqual(self.exports().count(), 1)


@override_settings(**REPORTING_DEFAULTS)
class RendererTests(SimpleTestCase):
    META = ReportMeta(title="Danger & <tags>", product_name="Brand",
                      lines=[("Data as of", "x")], data_as_of="x")

    def test_file_name_is_slug_and_report_tz_date(self):
        late_utc = datetime.datetime(2026, 11, 23, 22, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(export_filename("PMSv2 · Executive", "pdf", late_utc),
                         "pmsv2-executive-20261124.pdf")  # already the 24th in Riyadh
        with override_settings(DIS_REPORT_TZ="UTC"):
            self.assertEqual(export_filename("PMSv2 · Executive", "pdf", late_utc),
                             "pmsv2-executive-20261123.pdf")
        self.assertEqual(export_filename("مشروع", "csv", late_utc,
                                         fallback="dataset-7"), "dataset-7-20261124.csv")

    def test_format_stamp(self):
        self.assertEqual(format_stamp("2026-11-22T21:00:00+00:00"),
                         "2026-11-23 00:00 (Asia/Riyadh)")
        self.assertEqual(format_stamp("2026-11-22 21:00:00"), "2026-11-22 21:00")
        self.assertEqual(format_stamp("2026-11-22"), "2026-11-22")
        self.assertEqual(format_stamp("last night"), "last night")
        self.assertEqual(format_stamp(None), "")

    def test_csv_neutralises_formulas(self):
        out = render_csv(["a", "b"], [["=HYPERLINK(1)", -5], ["+1", "@x"]], self.META)
        text = out.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1),-5", text)
        self.assertIn("'+1,'@x", text)

    def test_xlsx_keeps_formula_and_error_lookalikes_as_text(self):
        out = render("xlsx", ["v"], [["=SUM(A1:A2)"], ["#N/A"], ["bad\x0bchar"], [3.5]], self.META)
        ws, header_row, _ = sheet_layout(out)
        cells = [ws.cell(header_row + i, 1) for i in range(1, 5)]
        self.assertEqual([c.value for c in cells], ["=SUM(A1:A2)", "#N/A", "badchar", 3.5])
        self.assertEqual([c.data_type for c in cells], ["s", "s", "s", "n"])

    def test_pdf_wraps_long_values_and_repeats_header(self):
        rows = [[i, "word " * 60, "x" * 400] for i in range(150)]
        out = render("pdf", ["n", "long_text", "unbroken"], rows, self.META)
        pages = PdfReader(io.BytesIO(out)).pages
        self.assertGreater(len(pages), 2)
        page2 = pages[1].extract_text()
        self.assertIn("long_text", page2)
        self.assertIn(f"Page 2 of {len(pages)}", page2)
        self.assertIn("Danger & <tags>", pages[0].extract_text())  # escaped, not markup

    def test_pdf_and_xlsx_handle_empty_results(self):
        self.assertTrue(render("pdf", ["a"], [], self.META).startswith(b"%PDF"))
        self.assertTrue(render("pdf", [], [], self.META).startswith(b"%PDF"))
        ws, header_row, _ = sheet_layout(render("xlsx", ["a"], [], self.META))
        self.assertEqual(ws.cell(header_row, 1).value, "a")

    def test_unknown_format_is_rejected(self):
        from core.report_render import UnsupportedFormat
        with self.assertRaises(UnsupportedFormat):
            render("docx", ["a"], [], self.META)
