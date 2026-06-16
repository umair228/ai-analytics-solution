"""
Run the DIS agent over the EGPC golden set, scoring the LIVE text-to-SQL path.

Same golden file + keyword scoring as ``eval_golden``, but it additionally tracks
whether the agent used ``ask_database`` and prints the exact SQL it ran — so you
can see Phase-1 "talk to the whole database" working and measure its accuracy.

    python manage.py eval_text2sql                 # full set
    python manage.py eval_text2sql --limit 5        # smoke
    python manage.py eval_text2sql --sql-only       # only questions answered via ask_database
    python manage.py eval_text2sql --out report.json

Needs the LLM configured and a live DataSource registered (point it at a safe
copy/replica, NOT production EGPC). See DIS_TalkToData_Plan.md §10.
"""
import json
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ai.agent import run_agent
from ai.client import active_model, is_configured
from ai.providers import get_provider


def _norm(text):
    return str(text or "").lower().replace(",", "")


def _sql_from_trace(trace):
    """Pull the SQL ask_database ran (last successful one), for display."""
    sqls = []
    for e in trace:
        if e.get("tool") == "ask_database":
            res = e.get("result") or {}
            if isinstance(res, dict) and res.get("sql"):
                sqls.append(res["sql"])
    return sqls[-1] if sqls else None


class Command(BaseCommand):
    help = "Run the agent over eval/golden_egpc.jsonl scoring the ask_database (text-to-SQL) path."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=None, help="Path to the golden jsonl.")
        parser.add_argument("--limit", type=int, default=None, help="Only the first N.")
        parser.add_argument("--steps", type=int, default=None, help="Override AGENT_MAX_STEPS.")
        parser.add_argument("--sql-only", action="store_true",
                            help="Report only questions the agent answered via ask_database.")
        parser.add_argument("--out", default=None, help="Write a JSON report here.")

    def handle(self, *args, **opts):
        path = Path(opts["file"]) if opts["file"] else Path(settings.BASE_DIR) / "eval" / "golden_egpc.jsonl"
        if not path.exists():
            raise CommandError(f"Golden set not found: {path}")

        provider = get_provider()
        self.stdout.write(f"provider={provider.name} model={active_model()}")
        if not is_configured():
            raise CommandError(f"LLM not configured: {provider.not_configured_message()}")

        User = get_user_model()
        user = (User.objects.filter(is_superuser=True).first()
                or User.objects.filter(role="admin").first() or User.objects.first())
        if not user:
            raise CommandError("No users exist — create an admin first.")

        items = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if opts["limit"]:
            items = items[: opts["limit"]]
        self.stdout.write(f"Running {len(items)} golden questions as '{user.username}'…\n")

        results = []
        n_pass = n_fail = n_review = 0
        n_via_sql = n_pass_via_sql = 0

        for i, q in enumerate(items, 1):
            t0 = time.monotonic()
            try:
                run = run_agent(user, [{"role": "user", "content": q["question"]}],
                                max_steps=opts["steps"])
                answer = run.answer or ""
                tools = [e["tool"] for e in run.trace]
                sql = _sql_from_trace(run.trace)
                err = None
            except Exception as exc:  # noqa: BLE001 — one question must not abort the run
                answer, tools, sql, err = "", [], None, str(exc)

            used_sql = "ask_database" in tools
            if opts["sql_only"] and not used_sql:
                continue

            kws = q.get("expect_keywords") or []
            ans_n = _norm(answer)
            hit = next((k for k in kws if _norm(k) in ans_n), None)

            if err:
                status = "ERROR"; n_fail += 1
            elif kws:
                status = "PASS" if hit else "FAIL"
                n_pass += status == "PASS"; n_fail += status == "FAIL"
            else:
                status = "REVIEW"; n_review += 1

            if used_sql:
                n_via_sql += 1
                n_pass_via_sql += status == "PASS"

            mark = {"PASS": "✓", "FAIL": "✗", "REVIEW": "?", "ERROR": "!"}[status]
            sql_tag = " [via SQL]" if used_sql else ""
            dt = time.monotonic() - t0
            self.stdout.write(f"[{i:>2}] {mark} {status:<6} ({dt:4.0f}s){sql_tag} {q['id']}  "
                              f"tools={tools or '—'}")
            if used_sql and sql:
                self.stdout.write(f"       SQL: {' '.join(sql.split())[:240]}")
            if status in ("FAIL", "ERROR", "REVIEW"):
                self.stdout.write(self.style.WARNING(f"       Q: {q['question']}"))
                self.stdout.write(f"       expected: {q.get('expected_answer','')[:160]}")
                self.stdout.write(f"       got:      {(err or answer)[:200]}")
            results.append({**q, "status": status, "matched": hit, "answer": answer,
                            "tools": tools, "used_sql": used_sql, "sql": sql, "error": err})

        scored = n_pass + n_fail
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Text-to-SQL scorecard ===  PASS {n_pass} / FAIL {n_fail} / REVIEW {n_review}"))
        if scored:
            self.stdout.write(f"Auto-scored accuracy: {n_pass}/{scored} = {100*n_pass/scored:.0f}%")
        self.stdout.write(f"Answered via ask_database: {n_via_sql} "
                          f"(of which PASS: {n_pass_via_sql})")

        if opts["out"]:
            Path(opts["out"]).write_text(json.dumps(
                {"model": active_model(), "total": len(results), "pass": n_pass,
                 "fail": n_fail, "review": n_review, "via_sql": n_via_sql,
                 "pass_via_sql": n_pass_via_sql, "results": results},
                indent=2, default=str), encoding="utf-8")
            self.stdout.write(f"\nReport written to {opts['out']}")
