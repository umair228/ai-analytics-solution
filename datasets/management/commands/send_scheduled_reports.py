"""Send scheduled CSV email reports for datasets.

Designed to be run periodically by cron, e.g. daily:

    0 6 * * * cd /path/to/backend && .venv/bin/python manage.py send_scheduled_reports
"""
import csv
import io
import logging
from datetime import timedelta

from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

from datasets.models import DatasetReport

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send scheduled dataset CSV email reports."

    def handle(self, *args, **options):
        now = timezone.now()
        sent = 0
        for report in DatasetReport.objects.filter(is_active=True).select_related(
            "dataset", "dataset__query", "dataset__query__datasource", "analysis_run"
        ):
            if not self._is_due(report, now):
                continue
            try:
                self._send(report)
                report.last_sent_at = now
                report.save(update_fields=["last_sent_at"])
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to send report %s: %s", report.id, exc)
        self.stdout.write(f"Sent {sent} report(s).")

    def _is_due(self, report, now):
        last = report.last_sent_at
        if last is None:
            return True
        if report.schedule == "daily" and (now - last) >= timedelta(days=1):
            return True
        if report.schedule == "weekly" and (now - last) >= timedelta(weeks=1):
            return True
        if report.schedule == "monthly" and (now - last) >= timedelta(days=30):
            return True
        return False

    def _send(self, report):
        recipients = [e.strip() for e in report.recipient_emails.split(",") if e.strip()]
        if not recipients:
            return
        if report.kind == DatasetReport.Kind.ANALYSIS_RUN:
            self._send_analysis_run(report, recipients)
        elif report.kind == DatasetReport.Kind.CHART:
            self._send_chart(report, recipients)
        else:
            self._send_dataset_csv(report, recipients)

    def _send_dataset_csv(self, report, recipients):
        from querybuilder.executor import execute_raw_sql, execute_spec
        from querybuilder.models import QueryDefinition

        dataset = report.dataset
        if dataset is None:
            raise ValueError("dataset_csv report has no dataset")
        query = dataset.query
        datasource = query.datasource

        if query.mode == QueryDefinition.Mode.RAW:
            result = execute_raw_sql(
                datasource, query.raw_sql, query.database or None,
                params=dataset.param_defaults or None,
            )
        else:
            result = execute_spec(datasource, query.spec, query.database or None)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(result.get("columns", []))
        writer.writerows(result.get("rows", []))

        msg = EmailMessage(
            subject=f"[DSE Report] {report.name}",
            body=(f"Scheduled {report.schedule} report for dataset '{dataset.name}'.\n"
                  f"Row count: {result.get('row_count', '?')}"),
            to=recipients,
        )
        msg.attach(f"{dataset.name}.csv", buf.getvalue(), "text/csv")
        msg.send()

    def _send_export(self, report, recipients, envelope, base_name):
        """Email an AnswerEnvelope in the report's format, falling back to CSV if
        an optional export dependency (e.g. WeasyPrint for PDF) is missing."""
        from ai.exporters import ExportUnavailable, export

        fmt = (report.export_format or "csv").lower()
        try:
            data, ctype, _ = export(envelope, fmt)
        except ExportUnavailable:
            fmt = "csv"
            data, ctype, _ = export(envelope, "csv")
        msg = EmailMessage(
            subject=f"[DSE Report] {report.name}",
            body=f"Scheduled {report.schedule} report: {report.name}.",
            to=recipients,
        )
        msg.attach(f"{base_name}.{fmt}", data, ctype)
        msg.send()

    def _send_analysis_run(self, report, recipients):
        from analytics.reporting import run_to_envelope

        run = report.analysis_run
        if run is None:
            raise ValueError("analysis_run report has no run attached")
        self._send_export(report, recipients, run_to_envelope(run),
                          base_name=(run.name or f"run_{run.id}").replace(" ", "_"))

    def _send_chart(self, report, recipients):
        from analytics.reporting import chart_to_envelope

        # A chart exports best as xlsx (native chart) unless told otherwise.
        if not (report.export_format or "").strip():
            report.export_format = "xlsx"
        self._send_export(report, recipients, chart_to_envelope(report.chart_config),
                          base_name=(report.name or "chart").replace(" ", "_"))
