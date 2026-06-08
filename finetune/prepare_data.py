"""
Build the DIS domain-adaptation fine-tuning dataset (Phase 6).

Generates chat-format SFT examples that teach a Qwen2.5 model the EGPC + petroleum-
QC + ASTM/ISO domain, grounded in REAL data already in the platform:

  * Glossary  -> definition Q&A (finetune/glossary.json)
  * EGPC facts-> Q&A from the seeded EGPC cached datasets (products, analyses,
                 sulphur, spec compliance, off-spec, instruments)
  * Standards -> grounded Q&A from the indexed ASTM/ISO corpus (docsearch.eval_astm
                 questions + retrieved passages)
  * Tool sense-> a few exemplars teaching which analysis answers which question

Output: finetune/data/domain_sft.jsonl  (one JSON object per line:
{"messages":[{"role":"system",...},{"role":"user",...},{"role":"assistant",...}]})

This RUNS locally (no GPU) — it only reads data and writes JSONL. Review/curate the
output, then train with finetune/train_qlora.py on the GPU box.

    .venv/bin/python finetune/prepare_data.py
"""
import json
import os
import sys
from pathlib import Path

import django

# This script lives in finetune/ — put the project root on the path so the
# Django `config` settings package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from datasets.models import Dataset  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "domain_sft.jsonl"

SYS = (
    "You are the DIS analytics assistant for the EGPC petroleum-quality "
    "laboratory. You understand LabWare LIMS data (samples, tests, results, "
    "specifications), petroleum-QC analyses, and ASTM/ISO standards. Answer "
    "accurately and concisely, grounded in the lab's data and standards."
)


def ex(user, assistant):
    return {"messages": [
        {"role": "system", "content": SYS},
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]}


def _ds(name):
    d = Dataset.objects.filter(name=name).first()
    if not d or not d.cached_rows:
        return None, None
    cols = [str(c) for c in (d.cached_columns or [])]
    return cols, d.cached_rows


def _col(cols, name):
    return cols.index(name) if cols and name in cols else -1


def glossary_examples():
    out = []
    data = json.loads((HERE / "glossary.json").read_text())
    for t in data["terms"]:
        out.append(ex(f"What is {t['term']} in petroleum quality testing?", t["definition"]))
        out.append(ex(f"Define {t['term']}.", t["definition"]))
    return out


def egpc_examples():
    out = []

    cols, rows = _ds("EGPC Samples by Product")
    if rows:
        ip, isam = _col(cols, "product"), _col(cols, "samples")
        top = sorted(rows, key=lambda r: -(r[isam] or 0))[:8]
        names = ", ".join(str(r[ip]) for r in top[:8])
        out.append(ex("Which products does the EGPC laboratory test most?",
                      f"The most-tested EGPC products by sample volume are: {names}."))
        for r in top[:6]:
            out.append(ex(f"How many samples has the EGPC lab logged for {r[ip]}?",
                          f"The EGPC lab has logged {r[isam]:,} samples for {r[ip]}."))

    cols, rows = _ds("EGPC Test Volume by Analysis")
    if rows:
        ia, ipct = _col(cols, "analysis"), _col(cols, "in_spec_pct")
        top = rows[:8]
        out.append(ex("What are the highest-volume analyses run in the EGPC lab?",
                      "The highest-volume EGPC analyses include: "
                      + ", ".join(str(r[ia]) for r in top[:8]) + "."))
        for r in top[:5]:
            if ipct >= 0 and r[ipct] is not None:
                out.append(ex(f"What is the in-spec rate for the {r[ia]} analysis?",
                              f"The {r[ia]} analysis has an in-spec rate of {r[ipct]}%."))

    cols, rows = _ds("EGPC Spec Compliance by Product")
    if rows:
        ip, ipct = _col(cols, "product"), _col(cols, "in_spec_pct")
        worst = sorted([r for r in rows if r[ipct] is not None], key=lambda r: r[ipct])[:5]
        if worst:
            out.append(ex("Which EGPC products have the lowest spec-compliance rate?",
                          "Lowest first-pass spec compliance: "
                          + "; ".join(f"{r[ip]} ({r[ipct]}%)" for r in worst) + "."))

    cols, rows = _ds("EGPC Sulphur by Product")
    if rows:
        ip, iavg, iu = _col(cols, "product"), _col(cols, "avg_sulphur"), _col(cols, "units")
        for r in rows[:8]:
            u = r[iu] if iu >= 0 and r[iu] else ""
            out.append(ex(f"What is the average sulphur content for {r[ip]} at EGPC?",
                          f"The average total-sulfur result for {r[ip]} is {r[iavg]} {u}."))

    cols, rows = _ds("EGPC Off-Spec by Operator")
    if rows:
        io, ipct = _col(cols, "operator"), _col(cols, "off_spec_pct")
        worst = sorted([r for r in rows if r[ipct] is not None], key=lambda r: -r[ipct])[:5]
        if worst:
            out.append(ex("Which analysts have the highest out-of-spec result rate at EGPC?",
                          "Highest off-spec rates: "
                          + "; ".join(f"{r[io]} ({r[ipct]}%)" for r in worst)
                          + ". This warrants review of method/technique, not blame."))

    cols, rows = _ds("EGPC Instruments & Calibration")
    if rows:
        out.append(ex("How many instruments are registered in the EGPC LIMS?",
                      f"There are {len(rows)} instruments registered, each with "
                      "calibration and preventive-maintenance dates tracked."))
    return out


def _egpc_conn():
    import pyodbc
    from decouple import config
    cs = (f"DRIVER={{{config('DB_DRIVER', default='ODBC Driver 18 for SQL Server')}}};"
          f"SERVER={config('DB_HOST', default='127.0.0.1')},{config('DB_PORT', default=1433)};"
          f"DATABASE={config('DB_NAME', default='EGPC_DEV')};"
          f"UID={config('DB_USER', default='SA')};PWD={config('DB_PASSWORD', default='')};"
          f"TrustServerCertificate=yes;Encrypt=no")
    return pyodbc.connect(cs, timeout=15)


def _r(x, p=4):
    try:
        return round(float(x), p)
    except (TypeError, ValueError):
        return None


def live_egpc_examples():
    """The rich source — grounded Q&A queried straight from the live EGPC_DEV DB:
    per-product analyses, per-analysis units/range/in-spec, numeric spec limits,
    sulphur, out-of-spec leaders, and operations facts."""
    out = []
    try:
        cn = _egpc_conn()
    except Exception as exc:  # noqa: BLE001
        print(f"  live EGPC DB unavailable ({exc}) — using cached datasets only.")
        return out
    c = cn.cursor()

    # 1. Products + sample volumes
    products = c.execute(
        "SELECT TOP 30 PRODUCT, COUNT(*) n FROM dbo.SAMPLE "
        "WHERE PRODUCT IS NOT NULL GROUP BY PRODUCT ORDER BY n DESC").fetchall()
    # Only a handful of these near-identical count Q&A (too many invites overfitting).
    for p, n in products[:8]:
        out.append(ex(f"How many samples has the EGPC lab tested for {p}?",
                      f"The EGPC laboratory has logged {n:,} samples for {p}."))
    if products:
        names = ", ".join(p for p, _ in products[:12])
        out.append(ex("What products does the EGPC laboratory test most?",
                      f"The most-tested EGPC products by sample volume are: {names}."))

    # 2. Per-product analyses
    for p, _ in products[:20]:
        ans = c.execute(
            "SELECT TOP 8 r.ANALYSIS, COUNT(*) n FROM dbo.RESULT r "
            "JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER=s.SAMPLE_NUMBER "
            "WHERE s.PRODUCT=? GROUP BY r.ANALYSIS ORDER BY n DESC", p).fetchall()
        if ans:
            out.append(ex(f"What analyses does the EGPC lab run on {p}?",
                          f"On {p}, EGPC most commonly runs these analyses: "
                          + ", ".join(a for a, _ in ans) + "."))

    # 3. Per-analysis: units, typical range, in-spec rate
    analyses = c.execute(
        "SELECT TOP 30 ANALYSIS, COUNT(*) n FROM dbo.RESULT "
        "WHERE NUMERIC_ENTRY IS NOT NULL GROUP BY ANALYSIS ORDER BY n DESC").fetchall()
    for a, _ in analyses:
        meta = c.execute(
            "SELECT TOP 1 NAME, UNITS FROM dbo.RESULT WHERE ANALYSIS=? "
            "AND UNITS IS NOT NULL AND UNITS<>''", a).fetchone()
        st = c.execute(
            "SELECT AVG(v), MIN(v), MAX(v), "
            "100.0*SUM(CASE WHEN insp='T' THEN 1 ELSE 0 END)/COUNT(*) FROM "
            "(SELECT TRY_CONVERT(float,NUMERIC_ENTRY) v, IN_SPEC insp FROM dbo.RESULT "
            " WHERE ANALYSIS=? AND NUMERIC_ENTRY IS NOT NULL) t WHERE v IS NOT NULL", a).fetchone()
        if not st or st[0] is None:
            continue
        name = meta[0] if meta else a
        units = (meta[1] if meta else "") or ""
        out.append(ex(
            f"What does the {a} analysis measure at EGPC, and what is its typical range?",
            f"{a} ({name}) is reported in {units}. Typical EGPC values average "
            f"{_r(st[0])} (range {_r(st[1])}–{_r(st[2])}), with an in-spec rate of "
            f"{_r(st[3],1)}%."))

    # 4. Numeric specification limits (the MOM 'numerical specifications')
    specs = c.execute(
        "SELECT TOP 50 PRODUCT, ANALYSIS, COMPONENT, "
        "TRY_CONVERT(float,LSL), TRY_CONVERT(float,USL), "
        "TRY_CONVERT(float,MIN_VALUE), TRY_CONVERT(float,MAX_VALUE), UNITS "
        "FROM dbo.PRODUCT_SPEC WHERE COALESCE(TRY_CONVERT(float,LSL),0)<>0 "
        "OR COALESCE(TRY_CONVERT(float,USL),0)<>0 "
        "OR COALESCE(TRY_CONVERT(float,MIN_VALUE),0)<>0 "
        "OR COALESCE(TRY_CONVERT(float,MAX_VALUE),0)<>0").fetchall()
    seen_spec = set()
    for p, a, comp, lsl, usl, mn, mx, u in specs:
        lo = lsl if (lsl not in (None, 0)) else mn
        hi = usl if (usl not in (None, 0)) else mx
        key = (p, comp)
        if key in seen_spec or (lo in (None, 0) and hi in (None, 0)):
            continue
        seen_spec.add(key)
        u = u or ""
        rng = (f"{_r(lo)}–{_r(hi)} {u}" if (lo not in (None, 0) and hi not in (None, 0))
               else f"max {_r(hi)} {u}" if hi not in (None, 0) else f"min {_r(lo)} {u}")
        out.append(ex(f"What is the EGPC specification limit for {comp} on {p}?",
                      f"The EGPC specification for {comp} ({a}) on {p} is {rng}."))

    # 5. Sulphur by product
    sulf = c.execute(
        "SELECT TOP 17 s.PRODUCT, AVG(TRY_CONVERT(float,r.NUMERIC_ENTRY)) avg_s, "
        "MAX(TRY_CONVERT(float,r.NUMERIC_ENTRY)) max_s, MAX(r.UNITS) u "
        "FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER=s.SAMPLE_NUMBER "
        "WHERE (r.ANALYSIS LIKE 'SULFUR%' OR r.NAME LIKE '%Sulfur%') "
        "AND r.NUMERIC_ENTRY IS NOT NULL AND s.PRODUCT IS NOT NULL "
        "GROUP BY s.PRODUCT ORDER BY avg_s DESC").fetchall()
    for p, avg_s, max_s, u in sulf:
        out.append(ex(f"What is the average sulphur content for {p} at EGPC?",
                      f"For {p}, the average total-sulfur result is {_r(avg_s)} {u or ''} "
                      f"(maximum observed {_r(max_s)})."))

    # 6. Worst out-of-spec products
    osp = c.execute(
        "SELECT TOP 6 s.PRODUCT, COUNT(*) n, "
        "100.0*SUM(CASE WHEN r.IN_SPEC='F' THEN 1 ELSE 0 END)/COUNT(*) offrate "
        "FROM dbo.RESULT r JOIN dbo.SAMPLE s ON r.SAMPLE_NUMBER=s.SAMPLE_NUMBER "
        "WHERE s.PRODUCT IS NOT NULL GROUP BY s.PRODUCT HAVING COUNT(*)>=50 "
        "ORDER BY offrate DESC").fetchall()
    if osp:
        lead = "; ".join(f"{p} ({_r(offrate,1)}% off-spec, n={n})" for p, n, offrate in osp[:5])
        out.append(ex("Which EGPC products have the worst out-of-spec rate?",
                      f"By individual result, the highest off-spec rates are: {lead}. "
                      "These warrant root-cause investigation."))

    # 7. Operations facts
    row = c.execute("SELECT COUNT(*), MIN(LOGIN_DATE), MAX(LOGIN_DATE) FROM dbo.SAMPLE").fetchone()
    if row:
        out.append(ex("How much sample data does the EGPC LIMS hold?",
                      f"The EGPC LIMS holds {row[0]:,} samples spanning "
                      f"{str(row[1])[:10]} to {str(row[2])[:10]}."))
    tat = c.execute(
        "SELECT AVG(CAST(DATEDIFF(hour,LOGIN_DATE,DATE_COMPLETED) AS float)) FROM dbo.SAMPLE "
        "WHERE DATE_COMPLETED IS NOT NULL AND DATEDIFF(hour,LOGIN_DATE,DATE_COMPLETED) BETWEEN 0 AND 8760").fetchone()
    if tat and tat[0] is not None:
        out.append(ex("What is the average sample turnaround time at EGPC?",
                      f"The average turnaround time (login to completion) is "
                      f"{_r(tat[0],1)} hours across completed EGPC samples."))
    cn.close()
    return out


def _fmt_method(m):
    """'ASTM_D_93' -> 'ASTM D93'; 'IP_501, IP_470' -> 'IP 501, IP 470'."""
    out = []
    for p in (m or "").split(","):
        p = (p.strip()
             .replace("ASTM_D_", "ASTM D").replace("ASTM_E_", "ASTM E")
             .replace("ASTM_SM_D", "ASTM D").replace("APHA_SM_", "APHA SM ")
             .replace("IP_", "IP ").replace("UOP_", "UOP ").replace("ES_", "ES ")
             .replace("_", " "))
        if p:
            out.append(p)
    return ", ".join(out)


def standards_link_examples():
    """The user's key ask — tie ASTM/ISO standards to EGPC's data, grounded in
    EGPC's OWN method assignments (ANALYSIS.METHOD) + spec limits + ISO concepts."""
    out = []
    try:
        cn = _egpc_conn()
    except Exception as exc:  # noqa: BLE001
        print(f"  standards-link skipped ({exc})")
        return out
    c = cn.cursor()

    # Real analysis ↔ method (from ANALYSIS.METHOD), highest-volume first
    rows = c.execute(
        "SELECT TOP 70 a.NAME, a.METHOD, a.REPORTED_NAME, rc.n FROM dbo.ANALYSIS a "
        "JOIN (SELECT ANALYSIS, COUNT(*) n FROM dbo.RESULT GROUP BY ANALYSIS) rc "
        "ON rc.ANALYSIS=a.NAME WHERE a.METHOD IS NOT NULL AND a.METHOD<>'' "
        "ORDER BY rc.n DESC").fetchall()
    methods_seen = set()
    for name, method, rep, _ in rows:
        fm = _fmt_method(method)
        rep = rep or name
        if not fm:
            continue
        out.append(ex(f"Which test method does the EGPC lab use for {rep}?",
                      f"EGPC measures {rep} ({name}) using {fm}."))
        first = fm.split(",")[0].strip()
        if first and first not in methods_seen and first.split()[0] in ("ASTM", "IP", "UOP"):
            methods_seen.add(first)
            out.append(ex(f"What does the {first} method measure in the EGPC laboratory?",
                          f"In the EGPC lab, {first} is used to determine {rep}."))

    # Spec + the method that tests it
    specs = c.execute(
        "SELECT TOP 40 ps.PRODUCT, ps.COMPONENT, ps.ANALYSIS, a.METHOD, "
        "TRY_CONVERT(float,ps.LSL), TRY_CONVERT(float,ps.USL), "
        "TRY_CONVERT(float,ps.MIN_VALUE), TRY_CONVERT(float,ps.MAX_VALUE), ps.UNITS "
        "FROM dbo.PRODUCT_SPEC ps LEFT JOIN dbo.ANALYSIS a ON a.NAME=ps.ANALYSIS "
        "WHERE COALESCE(TRY_CONVERT(float,ps.LSL),0)<>0 OR COALESCE(TRY_CONVERT(float,ps.USL),0)<>0 "
        "OR COALESCE(TRY_CONVERT(float,ps.MIN_VALUE),0)<>0 OR COALESCE(TRY_CONVERT(float,ps.MAX_VALUE),0)<>0").fetchall()
    seen = set()
    for p, comp, an, method, lsl, usl, mn, mx, u in specs:
        lo = lsl if lsl not in (None, 0) else mn
        hi = usl if usl not in (None, 0) else mx
        if (p, comp) in seen or (lo in (None, 0) and hi in (None, 0)):
            continue
        seen.add((p, comp))
        u = u or ""
        fm = _fmt_method(method) if method else ""
        rng = (f"{_r(lo)}–{_r(hi)} {u}" if (lo not in (None, 0) and hi not in (None, 0))
               else f"max {_r(hi)} {u}" if hi not in (None, 0) else f"min {_r(lo)} {u}")
        ans = f"EGPC's specification for {comp} on {p} is {rng}" + (f", tested by {fm}." if fm else ".")
        out.append(ex(f"What is EGPC's spec limit for {comp} on {p}, and which method tests it?", ans))
    cn.close()

    # ISO/ASTM concepts applied to EGPC (grounded, prose — not list format)
    out += [ex(q, a) for q, a in [
        ("How does ISO/IEC 17025 apply to EGPC's test results?",
         "Under ISO/IEC 17025, EGPC ensures metrological traceability of results to "
         "references, evaluates and reports measurement uncertainty, validates its "
         "methods, and applies documented decision rules when judging conformity to a "
         "product specification."),
        ("What do ISO 5725 repeatability and reproducibility mean for an EGPC analysis?",
         "Repeatability is the precision of repeat measurements by the same EGPC analyst "
         "on the same instrument over a short period; reproducibility is precision across "
         "different analysts or labs. Together they bound a result's expected scatter and "
         "feed its measurement uncertainty (ISO 21748)."),
        ("Per ASTM E1578, what must the EGPC LIMS guarantee for result data?",
         "ASTM E1578 (laboratory informatics) requires data integrity — secure electronic "
         "records, complete audit trails, user access control, and traceable result entry "
         "and approval — all provided by EGPC's LabWare LIMS."),
        ("When an EGPC result is near its spec limit, what does a decision rule (ISO 17025) require?",
         "A decision rule states how measurement uncertainty is accounted for at the limit "
         "(for example a guard band), so borderline results are judged consistently rather "
         "than on the raw value alone."),
    ]]
    return out


def reasoning_examples():
    """QC investigation/interpretation reasoning grounded in EGPC — teaches the model
    to REASON (and lean on the tools/standards), not just recite facts."""
    return [ex(q, a) for q, a in [
        ("Out-of-spec sulphur results suddenly rose for a product — how would you investigate?",
         "Check the sulphur analyser's calibration and re-run the QC check standards; "
         "review which analyst entered the results (ENTERED_BY) and any recent method or "
         "batch change; run a root-cause/driver analysis on the out-of-spec flag against "
         "product, analysis and operator; and confirm the result against the ASTM method "
         "(D4294/D5453). Treat it as a process signal, not blame."),
        ("A product's Cpk for density is 0.8 — what does that mean and what should EGPC do?",
         "Cpk 0.8 is below the 1.33 capability threshold, so the process is not capable — a "
         "non-trivial fraction of results will fall outside spec. EGPC should reduce process "
         "variation or re-centre the mean, and check instrument calibration and method "
         "repeatability (ISO 5725) before accepting the stream."),
        ("How should EGPC decide whether a turnaround-time problem is by section or by analyst?",
         "Compare average turnaround across sections (or process units), then drill into the "
         "worst one by analyst; an ANOVA across groups shows whether the differences are "
         "statistically significant rather than noise."),
        ("Which lens answers 'worst product for quality' — and why might two answers differ?",
         "By RATE, the worst product has the highest out-of-spec percentage (even if low "
         "volume); by VOLUME, the worst contributes the most failing results overall. Both "
         "are valid — cite the spec-compliance-by-product view for rate and a root-cause "
         "driver analysis for the biggest contributor."),
        ("How would you forecast next quarter's reagent or sample workload at EGPC?",
         "Resample the sample login dates into a monthly series and project it forward with a "
         "trend-plus-smoothing model, optionally split by process unit or product, then size "
         "staffing and reagents to the forecast plus a margin."),
    ]]


def general_examples():
    """A slice of general instruction data — keeps general ability intact and prevents
    the catastrophic forgetting / repetition-loops a domain-only fine-tune causes."""
    pairs = [
        ("What is 17 multiplied by 23?", "17 × 23 = 391."),
        ("Summarise the water cycle in one sentence.",
         "Water evaporates from surfaces, condenses into clouds, falls as precipitation, and flows back to seas and groundwater, repeating continuously."),
        ("Write a one-line Python function that returns the square of a number.",
         "def square(n): return n * n"),
        ("What is the capital of Japan?", "Tokyo."),
        ("Give a synonym for 'rapid'.", "Quick (also: fast, swift, speedy)."),
        ("Explain what a list is in programming, briefly.",
         "A list is an ordered, mutable collection of items accessed by index."),
        ("Convert 2.5 kilometres to metres.", "2.5 km = 2,500 metres."),
        ("What is the boiling point of water at sea level in Celsius?", "100 °C."),
        ("Write a polite one-sentence email opening to a colleague.",
         "I hope this message finds you well — I wanted to follow up on our recent discussion."),
        ("What is the past tense of 'run'?", "Ran."),
        ("Name three primary colours.", "Red, blue, and yellow."),
        ("What is 144 divided by 12?", "12."),
        ("Define 'photosynthesis' in one sentence.",
         "Photosynthesis is the process by which plants convert sunlight, water, and carbon dioxide into glucose and oxygen."),
        ("Give a one-sentence definition of 'algorithm'.",
         "An algorithm is a finite, well-defined sequence of steps for solving a problem."),
        ("What is the chemical symbol for gold?", "Au."),
        ("Rewrite 'The data was analysed by us' in active voice.", "We analysed the data."),
        ("What is 15% of 200?", "30."),
        ("List the days of the weekend.", "Saturday and Sunday."),
        ("Explain 'mean' vs 'median' in one sentence.",
         "The mean is the arithmetic average of values; the median is the middle value when they are sorted."),
        ("What is the largest planet in the solar system?", "Jupiter."),
        ("Write a Python one-liner to read a text file's lines into a list.",
         "lines = open('file.txt').read().splitlines()"),
        ("Round 3.14159 to two decimal places.", "3.14."),
        ("What does 'CPU' stand for?", "Central Processing Unit."),
        ("Give one tip for clear technical writing.",
         "Prefer short sentences and lead with the key point."),
        ("What is the freezing point of water in Fahrenheit?", "32 °F."),
        ("Translate 'good morning' into French.", "Bonjour."),
        ("What is a hypothesis, in one sentence?",
         "A hypothesis is a testable, falsifiable proposed explanation for an observation."),
        ("Sort these numbers ascending: 5, 2, 9, 1.", "1, 2, 5, 9."),
        ("What is the square root of 81?", "9."),
        ("Give a one-sentence definition of 'standard deviation'.",
         "Standard deviation measures how spread out values are around their mean."),
    ]
    return [ex(q, a) for q, a in pairs]


def standards_examples():
    out = []
    try:
        from docsearch.index_store import get_index
        idx = get_index()
        if not idx.ready or idx.df.empty:
            return out
        items = json.loads((HERE.parent / "docsearch" / "eval_astm.json").read_text())["items"]
        for it in items:
            rows, _ = idx.retrieve(it["question"], top_k=3)
            if not rows:
                continue
            r0 = rows[0]
            get = r0.get if hasattr(r0, "get") else (lambda k, d=None: r0[k])
            passage = " ".join((get("chunk_text", "") or "").split())[:480]
            src = get("doc_id", "") or "the standards"
            if len(passage) < 60:
                continue
            out.append(ex(it["question"], f"{passage} (Source: {src})"))
    except Exception as exc:  # noqa: BLE001
        print(f"  standards skipped: {exc}")
    return out


def tool_sense_examples():
    return [
        ex("How would you find why results are failing spec?",
           "Run a root-cause (driver) analysis on the out-of-spec flag (IN_SPEC='F') "
           "against factors like product, analysis and the operator who entered the "
           "result (ENTERED_BY) — it ranks each factor's lift over the base off-spec rate."),
        ex("How do you assess whether a process meets its sulphur specification?",
           "Compute process capability (Cp/Cpk) on the sulphur results versus the "
           "product's LSL/USL from PRODUCT_SPEC, and plot an individuals control chart; "
           "Cpk ≥ 1.33 indicates a capable process."),
        ex("How would you forecast next quarter's sample workload?",
           "Resample the SAMPLE login dates into a monthly time series and project it "
           "forward (trend + smoothing), optionally per process unit or product."),
    ]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    groups = {
        "glossary": glossary_examples(),
        "egpc_live": live_egpc_examples(),         # rich, from the live DB
        "standards_link": standards_link_examples(),  # standards ↔ EGPC data (real METHODs)
        "reasoning": reasoning_examples(),         # QC investigation/interpretation
        "general": general_examples(),             # anti-forgetting general data
        "egpc_cached": egpc_examples(),            # curated summaries (supplement)
        "standards": standards_examples(),         # ASTM/ISO corpus passages
        "tool_sense": tool_sense_examples(),
    }
    seen, examples = set(), []
    for items in groups.values():
        for e in items:
            q = e["messages"][1]["content"].strip().lower()
            if q in seen:
                continue
            seen.add(q)
            examples.append(e)

    with OUT.open("w", encoding="utf-8") as f:
        for e in examples:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"✓ wrote {len(examples)} SFT examples (deduped) -> {OUT}")
    print("\nbreakdown (pre-dedup):")
    for name, items in groups.items():
        print(f"  {name:12s}: {len(items)}")
    print("\nsample examples:")
    for e in (examples[0], examples[len(examples) // 2], examples[-1]):
        print(f"  Q: {e['messages'][1]['content']}")
        print(f"  A: {e['messages'][2]['content'][:130]}…\n")


if __name__ == "__main__":
    main()
