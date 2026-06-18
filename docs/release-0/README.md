# DSE Release 0 — Documentation Set

**Product:** Data Science Engine (DSE) — AI‑Powered Interactive & Agentic Analytics for LabWare LIMS
**Release:** 0 (customer‑demo / pilot) · **Target date:** 2026‑06‑21
**Scope of Release 0 features documented here:** standalone **Anomaly Detection**, **Statistical Calculations** and **Forecasting** workflows (with saved run history and local‑LLM explanations), a unified 10‑method forecasting engine, **Chart Studio** (multi‑dataset comparative charts) and **chart publishing/distribution** (export, email, scheduling), on top of the existing fully‑local LLM analytics platform.

These are **review‑ready drafts** authored against the implemented system. Tags in the text:
- `[SCREENSHOT]` — insert a UI capture.
- `[SITE]` — fill a site/customer‑specific value (URL, credentials, SMTP host…).
- `[SIGN‑OFF]` — requires a named reviewer signature/date.
- `[REVIEW: ML]` — needs AI/ML reviewer confirmation.

## Documents & ownership

| # | Document | File | Drafted | Owner to finalize |
|---|---|---|---|---|
| 1 | Requirements / Design | `01-requirements-design.md` | ✅ | Kulsoom (ML methodology) |
| 2 | User Manual | `02-user-manual.md` | ✅ | Zia (screenshots, walkthrough) |
| 3 | Use Cases | `03-use-cases.md` | ✅ | Zia (verify each) |
| 4 | Test Scripts / Test Cases | `04-test-scripts.md` | ✅ | Zia (execute black‑box, record results) |
| 5 | Installation Qualification (IQ) | `05-installation-qualification.md` | ✅ | Zia (run on prod, sign off) |
| 6 | Operational / Performance Qualification (OQ/PQ) | `06-oq-pq.md` | ✅ | Kulsoom (review + sign off) |
| 7 | Release Notes | `07-release-notes.md` | ✅ | Zia (format) |
| 8 | Deployment Guide | `08-deployment-guide.md` | ✅ | Zia (validate by deploying) |
| 9 | Technical Architecture | `09-technical-architecture.md` | ✅ | Zia (FE) + Kulsoom (AI/ML) |
| 10 | Cybersecurity | `10-cybersecurity.md` | ✅ | Zia (format) |

Seed material already in the repo: `DIS_ENTERPRISE_ARCHITECTURE.md`, `SECURITY.md`, `DIS_HANDOVER.md`, and `../../DIS_TalkToData_Plan.md`.

## Suggested finalization plan (to 2026‑06‑21)
- **Now → Sat:** Zia adds screenshots to the User Manual + Use Cases, executes the Test Scripts on the running app and records actual results, validates the Deployment Guide + IQ on the prod box, formats Release Notes / Cybersecurity to the house template.
- **Sun (Kulsoom back):** reviews Requirements/Design ML sections, signs off OQ/PQ (forecast‑accuracy & anomaly‑validation protocols), confirms the AI/ML section of the Technical Architecture.
- Convert finalized markdown to the customer's required format (Word/PDF) — the repo already produces PDFs from HTML for prior `DIS_*` docs.
