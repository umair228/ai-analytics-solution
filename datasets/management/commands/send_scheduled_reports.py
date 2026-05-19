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
            "dataset", "dataset__query", "dataset__query__datasource"
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
        from querybuilder.executor import execute_raw_sql, execute_spec
        from querybuilder.models import QueryDefinition

        dataset = report.dataset
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

        recipients = [e.strip() for e in report.recipient_emails.split(",") if e.strip()]
        msg = EmailMessage(
            subject=f"[DSE Report] {report.name}",
            body=(
                f"Scheduled {report.schedule} report for dataset '{dataset.name}'.\n"
                f"Row count: {result.get('row_count', '?')}"
            ),
            to=recipients,
        )
        msg.attach(f"{dataset.name}.csv", buf.getvalue(), "text/csv")
        msg.send()
