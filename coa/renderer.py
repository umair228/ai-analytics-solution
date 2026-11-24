"""Render a certificate as HTML (and PDF, when WeasyPrint is installed).

The document is designed around what must be impossible to miss:

* A DRAFT is stamped DRAFT and says it is not valid for release. An unsigned document
  that merely *looks* like a certificate is how an unapproved result leaves the lab.
* The verdict is the largest thing on the page, and INCOMPLETE is rendered as a
  failure state, not as a neutral one — because to a reader skimming for green, "we
  could not certify this" and "this passed" must never look alike.
* Missing tests are printed. A gap in a table is invisible; a row that says
  "ORDERED — NOT REPORTED" is not.
* A signature whose content hash no longer matches is shown, loudly, as INVALID.
"""
from __future__ import annotations

from html import escape

from ai.exporters import ExportUnavailable

VERDICT = {
    "conforms": ("CONFORMS TO SPECIFICATION", "pass"),
    "does_not_conform": ("DOES NOT CONFORM", "fail"),
    "incomplete": ("INCOMPLETE — CANNOT CERTIFY", "fail"),
}

CSS = """
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: "Helvetica Neue", Arial, sans-serif; color: #0f172a; font-size: 11pt; }
  .draft { position: fixed; top: 42%; left: 12%; font-size: 90pt; color: rgba(220,38,38,.10);
           transform: rotate(-24deg); font-weight: 800; letter-spacing: 6px; z-index: 0; }
  header { display: flex; justify-content: space-between; align-items: flex-start;
           border-bottom: 2px solid #0f172a; padding-bottom: 10px; }
  h1 { font-size: 17pt; margin: 0 0 2px; }
  .sub { color: #64748b; font-size: 9.5pt; }
  .num { text-align: right; font-family: monospace; font-size: 10pt; }
  .verdict { margin: 16px 0; padding: 12px 14px; border-radius: 6px; font-weight: 800;
             font-size: 14pt; letter-spacing: .5px; }
  .verdict.pass { background: #ecfdf3; color: #15803d; border-left: 6px solid #16a34a; }
  .verdict.fail { background: #fef2f2; color: #b91c1c; border-left: 6px solid #dc2626; }
  .verdict .why { display: block; font-weight: 400; font-size: 9.5pt; margin-top: 6px;
                  color: #0f172a; letter-spacing: 0; }
  .banner { padding: 9px 12px; border-radius: 6px; font-size: 9.5pt; margin-bottom: 12px; }
  .banner.warn { background: #fffbeb; color: #92400e; border-left: 4px solid #d97706; }
  .banner.bad  { background: #fef2f2; color: #b91c1c; border-left: 4px solid #dc2626; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0 16px; font-size: 9.5pt; }
  th { text-align: left; border-bottom: 1.5px solid #cbd5e1; padding: 6px 8px;
       text-transform: uppercase; font-size: 8pt; letter-spacing: .04em; color: #64748b; }
  td { border-bottom: 1px solid #e3e8ef; padding: 6px 8px; }
  tr.fail td { background: #fef2f2; }
  tr.missing td { background: #fffbeb; color: #92400e; font-weight: 600; }
  .chip { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 8.5pt;
          font-weight: 700; }
  .chip.p { background: #ecfdf3; color: #15803d; }
  .chip.f { background: #fef2f2; color: #b91c1c; }
  .chip.u { background: #f1f5f9; color: #475569; }
  .meta { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 4px 20px; font-size: 9.5pt;
          margin: 10px 0 4px; }
  .meta b { color: #64748b; font-weight: 600; }
  h2 { font-size: 11pt; margin: 18px 0 2px; }
  .sig { border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px 12px; margin-top: 6px; }
  .sig.invalid { border-color: #dc2626; background: #fef2f2; }
  .sig .who { font-weight: 700; }
  .sig .note { color: #b91c1c; font-weight: 700; font-size: 9pt; margin-top: 4px; }
  .unsigned { border: 1px dashed #cbd5e1; border-radius: 6px; padding: 12px;
              color: #64748b; font-size: 9.5pt; text-align: center; }
  footer { margin-top: 22px; padding-top: 8px; border-top: 1px solid #e3e8ef;
           color: #64748b; font-size: 8.5pt; line-height: 1.5; }
"""


def _chip(conforms):
    if conforms is True:
        return '<span class="chip p">PASS</span>'
    if conforms is False:
        return '<span class="chip f">FAIL</span>'
    # No in-spec decision was recorded. That is NOT a pass, and it must not read as one.
    return '<span class="chip u">NOT ADJUDICATED</span>'


def to_html(cert) -> str:
    c = cert.content or {}
    s = c.get("sample", {})
    label, tone = VERDICT.get(cert.verdict, ("UNKNOWN", "fail"))
    approved = cert.is_approved
    e = escape

    rows = []
    for r in c.get("results", []):
        cls = "fail" if r.get("conforms") is False else ""
        rows.append(
            f'<tr class="{cls}"><td>{e(str(r.get("analysis") or ""))}</td>'
            f'<td>{e(str(r.get("component") or ""))}</td>'
            f'<td>{e(str(r.get("value") if r.get("value") is not None else "—"))}</td>'
            f'<td>{e(str(r.get("units") or ""))}</td>'
            f'<td>{_chip(r.get("conforms"))}</td>'
            f'<td>{e(str(r.get("analyst") or ""))}</td></tr>'
        )
    # A gap in a table is invisible. A row that says ORDERED — NOT REPORTED is not.
    for m in c.get("missing_tests", []):
        rows.append(
            f'<tr class="missing"><td>{e(str(m.get("analysis") or ""))}</td>'
            f'<td colspan="3">Ordered — NOT REPORTED '
            f'({e(str(m.get("status_label") or ""))})</td>'
            f'<td><span class="chip f">MISSING</span></td><td>—</td></tr>'
        )
    for m in c.get("cancelled_tests", []):
        rows.append(
            f'<tr><td>{e(str(m.get("analysis") or ""))}</td>'
            f'<td colspan="3">Ordered — {e(str(m.get("status_label") or ""))} '
            f'(no result required)</td>'
            f'<td><span class="chip u">{e(str(m.get("status_label") or ""))}</span></td>'
            f'<td>—</td></tr>'
        )

    from .signing import manifestation
    sigs = manifestation(cert)
    sig_html = "".join(
        f'<div class="sig {"invalid" if not g["valid"] else ""}">'
        f'<div class="who">{e(g["meaning"])}: {e(g["printed_name"])}</div>'
        f'<div>{e(g["signed_at"].replace("T", " ")[:19])} UTC'
        + (f' · {e(g["reason"])}' if g["reason"] else "") + "</div>"
        + (f'<div class="note">{e(g["note"])}</div>' if g["note"] else "")
        + "</div>"
        for g in sigs
    ) or ('<div class="unsigned">Not signed. This document is a DRAFT and is '
          "not valid for release.</div>")

    banners = ""
    if cert.broken_signatures:
        banners += ('<div class="banner bad"><b>TAMPERED.</b> The content of this '
                    "certificate has changed since it was signed. The signature below "
                    "no longer matches the document and this certificate must not be "
                    "relied upon.</div>")
    if cert.status == "superseded":
        sup = cert.superseded_by.number if cert.superseded_by else "a later revision"
        banners += (f'<div class="banner warn"><b>SUPERSEDED</b> by {e(sup)}. '
                    "Do not rely on this revision.</div>")
    prov = c.get("provenance", {})
    if prov.get("freshness") == "stale":
        banners += ('<div class="banner warn"><b>Stale source data.</b> '
                    f'{e(str(prov.get("freshness_detail") or ""))}</div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{e(cert.number)}</title><style>{CSS}</style></head><body>
{'<div class="draft">DRAFT</div>' if not approved else ''}
<header>
  <div>
    <h1>Certificate of Analysis</h1>
    <div class="sub">DIS — Decision Intelligence Suite</div>
  </div>
  <div class="num">
    <div><b>{e(cert.number)}</b></div>
    <div>{e(cert.get_status_display())}</div>
  </div>
</header>

{banners}

<div class="verdict {tone}">{e(label)}
  <span class="why">{e(str(c.get("verdict_reason") or ""))}</span>
</div>

<div class="meta">
  <div><b>Sample</b><br>{e(s.get("text_id") or s.get("sample_number") or "")}</div>
  <div><b>Product</b><br>{e(s.get("product") or "—")}</div>
  <div><b>Type</b><br>{e(s.get("sample_type") or "—")}</div>
  <div><b>Logged</b><br>{e(str(s.get("logged_at") or "—")[:19].replace("T", " "))}</div>
  <div><b>Completed</b><br>{e(str(s.get("completed_at") or "—")[:19].replace("T", " "))}</div>
  <div><b>Status</b><br>{e(s.get("status_label") or s.get("status") or "—")}</div>
</div>

<h2>Results ({c.get("tests_reported", 0)} reported of {c.get("tests_ordered", 0)} ordered)</h2>
<table>
  <thead><tr><th>Analysis</th><th>Component</th><th>Result</th><th>Units</th>
  <th>Spec</th><th>Analyst</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="6">No results reported.</td></tr>'}</tbody>
</table>

<h2>Authorisation</h2>
{sig_html}

<footer>
  Source: {e(str((prov.get("datasource") or {}).get("name", "—")))} ·
  data as of {e(str(prov.get("data_as_of") or "live"))} ·
  issued {e(str(prov.get("generated_at") or "")[:19].replace("T", " "))} UTC<br>
  Every figure on this certificate is read directly from the laboratory information
  management system by a pre-approved query. No figure is produced by a language model.<br>
  Document integrity hash (SHA-256): {e(cert.content_hash)}
</footer>
</body></html>"""


def to_pdf(cert) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise ExportUnavailable(
            "PDF export requires WeasyPrint (pip install weasyprint). "
            "Use HTML and print to PDF, or install WeasyPrint on the server."
        ) from exc
    return HTML(string=to_html(cert)).write_pdf()
