"""
Run the DIS agent over the EGPC golden question set and print an accuracy
scorecard. This is the "is it actually answering correctly?" gate.

    python manage.py eval_golden                 # full set
    python manage.py eval_golden --limit 5        # first 5 (smoke)
    python manage.py eval_golden --out report.json

Reads eval/golden_egpc.jsonl. Each line has a question, the real expected answer
(computed from the EGPC data), and `expect_keywords` for automatic scoring.

Scoring per question:
  • PASS   — an expected keyword appears in the agent's answer
             (digits are comma-normalised, so "3,784" matches "3784").
  • REVIEW — a "company-gap"/"needs-data" question: we *want* the agent to admit
             the limitation rather than fabricate; flagged for human check
             (auto-PASS if it uses words like "insufficient/no data/cannot").
  • FAIL   — keywords expected but none found.

Needs the LLM configured (`llm_check`) and the EGPC datasets registered, so run
it on the server once the data link is live.
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

_LIMIT_SIGNALS = ("insufficient", "not enough", "no data", "cannot", "can't",
                  "unable", "not available", "no company", "lack")


def _norm(text):
    return str(text or "").lower().replace(",", "")


class Command(BaseCommand):
    help = "Run the agent over eval/golden_egpc.jsonl and print an accuracy scorecard."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=None, help="Path to the golden jsonl.")
        parser.add_argument("--limit", type=int, default=None, help="Only the first N.")
        parser.add_argument("--steps", type=int, default=None, help="Override AGENT_MAX_STEPS.")
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

        results, by_cap = [], {}
        n_pass = n_fail = n_review = 0
        for i, q in enumerate(items, 1):
            t0 = time.monotonic()
            try:
                run = run_agent(user, [{"role": "user", "content": q["question"]}],
                                max_steps=opts["steps"])
                answer = run.answer or ""
                tools = [e["tool"] for e in run.trace]
                err = None
            except Exception as exc:  # noqa: BLE001 — one question must not abort the run
                answer, tools, err = "", [], str(exc)

            kws = q.get("expect_keywords") or []
            ans_n = _norm(answer)
            hit = next((k for k in kws if _norm(k) in ans_n), None)
            flag = q.get("answerable", "yes")

            if err:
                status = "ERROR"; n_fail += 1
            elif kws:
                status = "PASS" if hit else "FAIL"
                n_pass += status == "PASS"; n_fail += status == "FAIL"
            elif flag in ("company-gap", "needs-data"):
                # want an honest limitation, not a fabricated answer
                status = "PASS" if any(s in ans_n for s in _LIMIT_SIGNALS) else "REVIEW"
                n_pass += status == "PASS"; n_review += status == "REVIEW"
            else:
                status = "REVIEW"; n_review += 1

            cap = q.get("capability", "?")
            by_cap.setdefault(cap, [0, 0])
            by_cap[cap][0] += status == "PASS"
            by_cap[cap][1] += 1

            mark = {"PASS": "✓", "FAIL": "✗", "REVIEW": "?", "ERROR": "!"}[status]
            dt = time.monotonic() - t0
            self.stdout.write(f"[{i:>2}] {mark} {status:<6} ({dt:4.0f}s) {q['id']}  "
                              f"tools={tools or '—'}")
            if status in ("FAIL", "ERROR", "REVIEW"):
                self.stdout.write(self.style.WARNING(
                    f"       Q: {q['question']}"))
                self.stdout.write(f"       expected: {q.get('expected_answer','')[:160]}")
                self.stdout.write(f"       got:      {(err or answer)[:200]}")
            results.append({**q, "status": status, "matched": hit, "answer": answer,
                            "tools": tools, "error": err})

        total = len(items)
        scored = n_pass + n_fail
        self.stdout.write(self.style.SUCCESS(
            f"\n=== Scorecard ===  PASS {n_pass} / FAIL {n_fail} / REVIEW {n_review}"))
        if scored:
            self.stdout.write(f"Auto-scored accuracy: {n_pass}/{scored} = {100*n_pass/scored:.0f}%"
                              f"  (REVIEW {n_review} need a human look)")
        self.stdout.write("By capability:")
        for cap, (p, t) in sorted(by_cap.items()):
            self.stdout.write(f"  {cap:<16} {p}/{t}")

        if opts["out"]:
            Path(opts["out"]).write_text(json.dumps(
                {"model": active_model(), "total": total, "pass": n_pass,
                 "fail": n_fail, "review": n_review, "results": results},
                indent=2, default=str), encoding="utf-8")
            self.stdout.write(f"\nReport written to {opts['out']}")
