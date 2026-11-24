"""Send scheduled email reports: dataset tables, saved analysis runs, charts.

Run it from the scheduler loop or cron — it only sends what is due, e.g.:

    */5 * * * * cd /path/to/backend && .venv/bin/python manage.py send_scheduled_reports

* A dataset report attaches the dataset's FULL result in the report's
  ``export_format`` (csv, xlsx or pdf), rendered by the same branded exporters
  and row ceilings as GET /api/datasets/<id>/export/. Analysis-run and chart
  reports keep the envelope exporters for csv/xlsx/html; their pdf is rendered
  with ReportLab. A render that fails fails the send — no silent fallback to
  another format.
* Each recipient gets their own message. Before it goes to SMTP a SEND audit
  row (status "pending") is committed; if that row cannot be written the send
  is skipped and an error logged (fail-closed). The row is then marked "sent"
  or "failed".
* The report owner must still be able to view the dataset (same rule as the
  API); otherwise the report is not sent.
* Timing (unset = the original behaviour):
    DIS_REPORT_SEND_HOUR        local hour (DIS_REPORT_TZ) from which reports go
                                out; daily/weekly then count calendar days
    DIS_REPORT_CALENDAR_MONTHS  monthly = once per calendar month (from the 1st)
                                instead of every 30 days
    DIS_EMAIL_SUBJECT_PREFIX    subject prefix (default "[DSE Report]")
"""
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.audit import SEND, AuditWriteError, record_audit_strict
from core.branding import footer, product_name
from core.models import AuditLog
from core.report_render import (
    FORMATS, ReportMeta, format_timestamp, render, report_tz,
)
from datasets.models import DatasetReport

logger = logging.getLogger(__name__)

_PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def is_due(report, now) -> bool:
    """Whether ``report`` should go out at ``now`` (an aware datetime)."""
    local_now = now.astimezone(report_tz())
    send_hour = settings.DIS_REPORT_SEND_HOUR
    if send_hour is not None and local_now.hour < send_hour:
        return False
    last = report.last_sent_at
    if last is None:
        return True
    local_last = last.astimezone(report_tz())
    if report.schedule == DatasetReport.Schedule.MONTHLY and settings.DIS_REPORT_CALENDAR_MONTHS:
        return (local_last.year, local_last.month) < (local_now.year, local_now.month)
    days = _PERIOD_DAYS.get(report.schedule)
    if days is None:
        return False
    if send_hour is not None:
        # Calendar days, anchored at the send hour: no drift with run times.
        return (local_now.date() - local_last.date()).days >= days
    return (now - last) >= timedelta(days=days)


def _signature() -> str:
    """A signature only for a deployment that configured a brand; otherwise
    the email reads as it always has."""
    parts = [p for p in ((settings.DIS_BRAND_PRODUCT_NAME or "").strip(), footer()) if p]
    return ("\n\n-- \n" + "\n".join(parts)) if parts else ""


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str
    body: str
    detail: dict  # audit detail shared by every recipient's row


class Command(BaseCommand):
    help = "Send the scheduled email reports that are due."

    def handle(self, *args, **options):
        now = timezone.now()
        sent = 0
        reports = DatasetReport.objects.filter(is_active=True).select_related(
            "dataset", "dataset__query", "dataset__query__datasource",
            "analysis_run", "owner")
        for report in reports:
            if not is_due(report, now):
                continue
            try:
                delivered = self._send(report, now)
            except Exception as exc:  # noqa: BLE001 — one report must not stop the rest
                logger.error("Failed to send report %s: %s", report.id, exc)
                self.stderr.write(f"Failed '{report.name}': {exc}")
                continue
            # delivered: None = no recipients (nothing to retry); 0 = every
            # recipient failed (retry next run); n = sent to n recipients.
            if delivered == 0:
                continue
            report.last_sent_at = now
            report.save(update_fields=["last_sent_at"])
            if delivered:
                sent += 1
        self.stdout.write(f"Sent {sent} report(s).")

    # ---- one report ------------------------------------------------------

    def _send(self, report, now):
        recipients = [e.strip() for e in (report.recipient_emails or "").split(",")
                      if e.strip()]
        if not recipients:
            logger.warning("Report %s has no recipients; skipped.", report.id)
            return None
        if report.kind == DatasetReport.Kind.ANALYSIS_RUN:
            attachment = self._run_attachment(report, now)
        elif report.kind == DatasetReport.Kind.CHART:
            attachment = self._chart_attachment(report, now)
        else:
            attachment = self._dataset_attachment(report, now)
        return self._deliver(report, recipients, attachment)

    def _deliver(self, report, recipients, attachment) -> int:
        subject = f"{settings.DIS_EMAIL_SUBJECT_PREFIX} {report.name}".strip()
        delivered = 0
        for recipient in recipients:
            detail = {**attachment.detail, "recipient": recipient,
                      "subject": subject, "status": "pending"}
            try:
                row = record_audit_strict(
                    SEND, user=report.owner, target_type="DatasetReport",
                    target_id=report.id,
                    summary=f"Scheduled report '{report.name}' to {recipient}",
                    detail=detail,
                )
            except AuditWriteError as exc:
                logger.error("Report %s NOT sent to %s: the audit row could not be "
                             "written (%s).", report.id, recipient, exc)
                self.stderr.write(f"Not sent '{report.name}' to {recipient}: audit failed.")
                continue
            message = EmailMessage(subject=subject, body=attachment.body, to=[recipient])
            message.attach(attachment.filename, attachment.content, attachment.mimetype)
            try:
                message.send()
            except Exception as exc:  # noqa: BLE001
                logger.error("Report %s: email to %s failed: %s", report.id, recipient, exc)
                self._set_status(row, detail, "failed", str(exc))
                continue
            self._set_status(row, detail, "sent")
            delivered += 1
        return delivered

    @staticmethod
    def _set_status(row, detail, status, error=""):
        new = {**detail, "status": status}
        if error:
            new["error"] = error[:500]
        try:
            AuditLog.objects.filter(pk=row.pk).update(detail=new)
        except Exception as exc:  # noqa: BLE001 — the pending row already proves the attempt
            logger.warning("Could not mark audit row %s as %s: %s", row.pk, status, exc)

    # ---- attachments -----------------------------------------------------

    def _dataset_attachment(self, report, now) -> Attachment:
        from datasets.exports import export_dataset, user_label

        dataset = report.dataset
        if dataset is None:
            raise ValueError("dataset report has no dataset")
        owner = report.owner
        if owner is not None and not (owner.is_active and dataset.accessible_by(owner)):
            raise PermissionError(
                f"owner '{owner.username}' can no longer view dataset '{dataset.name}'")
        fmt = (report.export_format or "csv").lower()
        if fmt not in FORMATS:
            raise ValueError(f"export_format '{fmt}' is not supported for a dataset "
                             f"report; use one of: {', '.join(FORMATS)}.")
        by = f"scheduled report '{report.name}'"
        if owner is not None:
            by += f" for {user_label(owner)}"
        out = export_dataset(dataset, fmt, generated_by=by, now=now)
        stamps = [f"{k}: {v}" for k, v in out.meta.lines
                  if k in ("Data as of", "Source data as of")]
        body = "\n".join([f"Scheduled {report.schedule} report for dataset "
                          f"'{dataset.name}'.", f"Row count: {out.row_count}",
                          *stamps]) + _signature()
        detail = {"kind": report.kind, "report": report.name, "dataset_id": dataset.id,
                  **out.audit_detail(dataset)}
        return Attachment(out.filename, out.content, out.content_type.split(";")[0],
                          body, detail)

    def _run_attachment(self, report, now) -> Attachment:
        from analytics.reporting import run_to_envelope

        run = report.analysis_run
        if run is None:
            raise ValueError("analysis_run report has no run attached")
        return self._envelope_attachment(
            report, run_to_envelope(run), now,
            base_name=(run.name or f"run_{run.id}").replace(" ", "_"),
            fmt=(report.export_format or "csv").lower())

    def _chart_attachment(self, report, now) -> Attachment:
        from analytics.reporting import chart_to_envelope

        # A chart exports best as xlsx (native chart) unless told otherwise.
        return self._envelope_attachment(
            report, chart_to_envelope(report.chart_config), now,
            base_name=(report.name or "chart").replace(" ", "_"),
            fmt=(report.export_format or "").strip().lower() or "xlsx")

    def _envelope_attachment(self, report, envelope, now, *, base_name, fmt) -> Attachment:
        result = envelope.get("result") or {}
        columns, rows = result.get("columns") or [], result.get("rows") or []
        if fmt == "pdf":
            content, mimetype = self._envelope_pdf(report, envelope, columns, rows, now), \
                "application/pdf"
        else:
            # csv / xlsx / html keep the envelope exporters (native chart and
            # provenance sheet in xlsx); an unknown format raises — no fallback.
            from ai.exporters import export
            content, mimetype, _ = export(envelope, fmt)
        filename = f"{base_name}.{fmt}"
        detail = {"kind": report.kind, "report": report.name, "format": fmt,
                  "filename": filename, "row_count": len(rows), "bytes": len(content)}
        body = f"Scheduled {report.schedule} report: {report.name}." + _signature()
        return Attachment(filename, content, mimetype, body, detail)

    @staticmethod
    def _envelope_pdf(report, envelope, columns, rows, now) -> bytes:
        from ai.exporters import _provenance_lines
        from datasets.exports import ExportTooLarge

        limit = settings.DIS_EXPORT_PDF_MAX_ROWS
        if len(rows) > limit:
            raise ExportTooLarge("pdf", limit)
        generated_at = format_timestamp(now)
        by = f"scheduled report '{report.name}'"
        lines = [(str(k).replace("_", " ").capitalize(), str(v))
                 for k, v in _provenance_lines(envelope)]
        lines += [("Generated by", by), ("Generated at", generated_at),
                  ("Rows", f"{len(rows):,}")]
        meta = ReportMeta(title=report.name, product_name=product_name(),
                          footer=footer(), lines=lines, generated_by=by,
                          generated_at=generated_at,
                          note=str(envelope.get("narrative") or ""))
        return render("pdf", columns, rows, meta)
