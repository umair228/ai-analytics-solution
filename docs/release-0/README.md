# DSE Release 0 — Documentation Set

**Product:** Data Science Engine (DSE) — AI‑Powered Insights for Modern Laboratories (LabWare LIMS)
**Release:** 0 (customer‑demo / pilot) · **Target date:** 2026‑06‑21
**Scope documented here:** standalone **Anomaly Detection**, **Statistical Calculations** and **Forecasting** workflows (saved run history + local‑LLM explanations), a unified 10‑method forecasting engine, **Chart Studio** (multi‑dataset comparative charts) and **chart publishing/distribution** (export, email, scheduling) — on top of the existing fully‑local LLM analytics platform.

These are **review‑ready drafts** authored against the implemented system. Tags in the text mark where each owner adds the final touches:
- `[SCREENSHOT]` — insert a UI capture.
- `[SITE]` — fill a site/customer‑specific value (URL, credentials, SMTP host…).
- `[SIGN‑OFF]` — requires a named reviewer signature/date.
- `[REVIEW: ML]` — methodology to be confirmed by an AI/ML‑literate reviewer.

## Documents & owners (per assignment)

| # | Document | File | Owner(s) |
|---|---|---|---|
| 1 | Requirements / Design Document | `01-requirements-design.md` | **Muhammad Naseem** |
| 2 | User Manual | `02-user-manual.md` | **Kulsoom** |
| 3 | Use Cases / Demo Scripts | `03-use-cases.md` | **Kulsoom** |
| 4 | Test Scripts / Test Cases | `04-test-scripts.md` | **Zia Arshad** |
| 5 | Installation Qualification (IQ) | `05-installation-qualification.md` | **Umair Khan** |
| 6 | Operational Qualification (OQ) | `06-oq-pq.md` → Part A | **Zia Arshad** |
| 7 | Performance Qualification (PQ) | `06-oq-pq.md` → Part B | **Zia Arshad** |
| 8 | Release Notes / Product Roadmap | `07-release-notes.md` | **Muhammad Naseem / Umair Khan** |
| 9 | Deployment Guide | `08-deployment-guide.md` | **Muhammad Naseem / Umair Khan** |
| 10 | Technical Architecture Document | `09-technical-architecture.md` | **Muhammad Naseem / Umair Khan** |
| 11 | Cybersecurity Document | `10-cybersecurity.md` | **Muhammad Naseem** |

> OQ and PQ are two deliverables in one file (`06-oq-pq.md`, Part A = OQ, Part B = PQ), both owned by Zia Arshad — split into two files on request.

## What each person owns

- **Muhammad Naseem** — Requirements/Design (1), Cybersecurity (11), and co‑owns Release Notes/Roadmap (8), Deployment Guide (9), Technical Architecture (10) with Umair. Seed docs to reuse: `../../DIS_ENTERPRISE_ARCHITECTURE.md`, `SECURITY.md`, `../../DIS_TalkToData_Plan.md`.
- **Umair Khan** — Installation Qualification (5); co‑owns 8/9/10 with Muhammad. Best placed for the deploy/IQ steps on the prod host (`dis-deploy`).
- **Zia Arshad** — Test Scripts/Cases (4), OQ (6), PQ (7). Execute the black‑box scripts on the running app, record actual results + pass/fail, and run the OQ/PQ protocols.
- **Kulsoom** — User Manual (2), Use Cases/Demo Scripts (3). Add screenshots + verify each flow clicks through; the Demo Scripts double as the customer walkthrough. (Also the natural `[REVIEW: ML]` reviewer for methodology in docs 1 and 6/7 when back on Sunday.)

## Suggested sequence (to 2026‑06‑21)
- **Now → Sat:** Zia executes Test Scripts + OQ/PQ on the app and records results; Umair validates IQ + Deployment on the prod host; Muhammad finalizes Requirements/Design, Cybersecurity, Release Notes/Roadmap, and the Technical Architecture (FE/BE narrative).
- **Sun (Kulsoom back):** User Manual + Use Cases/Demo Scripts screenshots & walkthrough; ML methodology review of docs 1 and 6/7.
- Convert finalized markdown to the customer's required format (Word/PDF) — the repo already produces PDFs from HTML for prior `DIS_*` docs.
