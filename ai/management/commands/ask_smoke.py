"""Fast end-to-end smoke of the live text-to-SQL path against a real datasource.

Runs each question straight through ``texttosql.run_text_to_sql`` (the ask_database
path — NOT the full agent), so it isolates and shows exactly what SQL was generated
(or which certified metric fired) and whether the returned data contains the
expected answer. The tightest feedback loop for "does it actually work on EGPC".

    python manage.py ask_smoke --datasource EGPC_DEV
    python manage.py ask_smoke --datasource 2 --golden          # the 21 golden questions
    python manage.py ask_smoke --datasource 2 --file my.jsonl    # custom {question, expect_keywords}

Point --datasource at the REPLICA in production (never the live source). Questions
that hit a certified metric need no LLM; generated ones need LLM_BASE_URL configured.
See DIS_TalkToData_Activation.md.
"""
import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from semantics.management.commands.load_semantic_layer import resolve_datasource
from texttosql.runner import run_text_to_sql

# Representative EGPC questions. `expect` (optional) = substrings that must appear
# in the returned columns/rows for a PASS.
DEFAULT_QUESTIONS = [
    {"question": "How many samples are in the database in total?"},
    {"question": "Which product has the highest out-of-specification rate?",
     "expect_keywords": ["PROPANE"]},
    {"question": "Show the count of off-spec results grouped by the operator who entered them."},
    {"question": "What is the average sulphur value for each product?"},
    {"question": "How many samples are there per product?"},
    # Exercises the do_not_group guard (INSTRUMENT is empty in EGPC):
    {"question": "List out-of-spec results broken down by instrument."},
]


def _norm(s):
    return str(s or "").lower().replace(",", "")


def _haystack(result):
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    parts = [str(c) for c in cols]
    for r in rows[:50]:
        parts += ["" if c is None else str(c) for c in r]
    return _norm(" ".join(parts))


class Command(BaseCommand):
    help = "Smoke-test the live text-to-SQL path on a datasource (prints SQL + pass/fail)."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", required=True, help="DataSource id or name.")
        parser.add_argument("--database", default=None, help="Specific database/catalog.")
        parser.add_argument("--golden", action="store_true",
                            help="Use eval/golden_egpc.jsonl instead of the built-in set.")
        parser.add_argument("--file", default=None, help="Custom jsonl of {question, expect_keywords}.")
        parser.add_argument("--limit", type=int, default=None, help="Only the first N questions.")

    def _load_questions(self, opts):
        path = opts["file"]
        if opts["golden"] and not path:
            path = Path(settings.BASE_DIR) / "eval" / "golden_egpc.jsonl"
        if path:
            if not Path(path).exists():
                raise CommandError(f"Question file not found: {path}")
            items = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
                     if l.strip()]
        else:
            items = list(DEFAULT_QUESTIONS)
        return items[: opts["limit"]] if opts["limit"] else items

    def handle(self, *args, **opts):
        ds = resolve_datasource(opts["datasource"])
        items = self._load_questions(opts)
        self.stdout.write(f"Smoke-testing {len(items)} questions on '{ds.name}' "
                          f"[{ds.source_type}]…\n")

        n_pass = n_fail = n_noexp = n_err = 0
        paths = {}
        for i, q in enumerate(items, 1):
            question = q["question"]
            expect = q.get("expect_keywords") or q.get("expect") or []
            t0 = time.monotonic()
            try:
                res = run_text_to_sql(ds, question, database=opts["database"])
                err = None
            except Exception as exc:  # noqa: BLE001 — one question must not abort the run
                res, err = {}, str(exc)

            dt = time.monotonic() - t0
            path = res.get("path", "error" if err else "?")
            paths[path] = paths.get(path, 0) + 1

            if err:
                status = "ERROR"
                n_err += 1
            elif not res.get("understood", False):
                status = "FAIL"
                n_fail += 1
            elif expect:
                hit = next((k for k in expect if _norm(k) in _haystack(res)), None)
                status = "PASS" if hit else "FAIL"
                n_pass += status == "PASS"
                n_fail += status == "FAIL"
            else:
                status = "OK"
                n_noexp += 1

            mark = {"PASS": "✓", "OK": "·", "FAIL": "✗", "ERROR": "!"}[status]
            self.stdout.write(f"[{i:>2}] {mark} {status:<5} ({dt:4.1f}s) path={path} "
                              f"rows={res.get('row_count', '-')}")
            self.stdout.write(self.style.HTTP_INFO(f"     Q: {question}"))
            if res.get("sql"):
                self.stdout.write(f"     SQL: {' '.join(res['sql'].split())[:240]}")
            rows = res.get("rows") or []
            if rows:
                self.stdout.write(f"     → {res.get('columns')}  {rows[0]}")
            if err:
                self.stdout.write(self.style.ERROR(f"     ERROR: {err[:200]}"))
            elif not res.get("understood", True):
                self.stdout.write(self.style.WARNING(f"     {res.get('error', '')[:200]}"))

        scored = n_pass + n_fail
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Smoke ===  PASS {n_pass} / FAIL {n_fail} / no-expect {n_noexp} / ERROR {n_err}"))
        if scored:
            self.stdout.write(f"Keyword accuracy: {n_pass}/{scored} = {100*n_pass/scored:.0f}%")
        self.stdout.write("Paths: " + ", ".join(f"{k}={v}" for k, v in sorted(paths.items())))
