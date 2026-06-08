# DIS Eval — EGPC accuracy scoreboard

Turns the LabWare Arabia question document into a **measurable accuracy gate** for
DIS: real lab questions → real expected answers (computed from the EGPC data) →
a pass/fail scorecard against the live agent.

## Files
| File | What |
|---|---|
| [egpc_question_catalog.md](egpc_question_catalog.md) | All ~180 questions mapped to capability + **data-readiness** (incl. the company-data gap) |
| [golden_egpc.jsonl](golden_egpc.jsonl) | Scored subset: question + **real expected answer** + keywords + flag |
| `ai/management/commands/eval_golden.py` | Runs the agent over the golden set → scorecard |

## The key finding (carry into the meeting)
The document assumes a **multi-company "across EGPC"** dataset, but the current
data is essentially **one organisation's** lab ops: `SAMPLE.CUSTOMER` is set on
only ~7% of samples (11 values, NORPETCO-dominant). So the many **"by company /
top-N companies"** questions are a **data requirement**, not an AI gap. What's
fully answerable now: **OOS (all angles), products, tests/methods, parameters,
sample types, statuses, ranges, TAT, trends.**

## Run it (on the server, once the data link is live)
```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec api \
  python manage.py eval_golden            # full set
#  add  --limit 5   for a quick smoke,   --out report.json   to save
```
Prereqs: `llm_check` ✅ and the EGPC datasets registered (so the agent can query).

## How scoring works
- **PASS** — an expected keyword appears in the agent's answer (digits are
  comma-normalised, so `3,784` matches `3784`).
- **REVIEW** — a `company-gap` / `needs-data` question: we *want* the agent to
  **admit the limitation** rather than invent companies (auto-PASS if it says
  "insufficient/no data/cannot"; otherwise flagged for a human look — inventing an
  answer here is a *fail* you want to catch).
- **FAIL** — keywords expected, none found.

`expected_answer` is the human ground truth; keyword matching is the first-pass
automation.

## Important caveat — dataset scoping
Ground truth is computed from the **full** EGPC dump. The deployed agent answers
over the **registered datasets**, some of which are **sampled/filtered** (e.g. a
representative `WHERE RESULT_NUMBER % 49 = 0`). So if the agent's number diverges
from the golden value, first check whether the underlying **dataset definition**
is sampled — it may be the data scope, not the model. (Great diagnostic signal.)

## Where this plugs in
- Retrieval quality: `python manage.py eval_astm`
- Served-model quality: `python finetune/eval_domain.py`
- **End-to-end agent accuracy: this (`eval_golden`)**

Next: grow the golden set toward ~40 across more sections, fill exact analysis
codes for Sulphur/RVP/Paraffins ranges, and wire `eval_golden` into CI as a
gate before any prompt/model/embedder change ships.
