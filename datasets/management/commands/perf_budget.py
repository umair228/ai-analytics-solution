"""Board performance budget: time every board's server-side calls (P10).

    manage.py perf_budget --datasource "PMSv2 Reporting"            # as viewers see it
    manage.py perf_budget --datasource "PMSv2 Reporting" --cold     # bypass the cache
    manage.py perf_budget --budget-ms 3000 --runs 5 --json perf.json

A board "loads" when the last of its database calls returns; the calls run six at a
time, as a browser does against one host. Each board is loaded ``--runs`` times and
its p95 compared with the budget (default DIS_BOARD_BUDGET_MS, 3000 ms: the R1
criterion is 3 s per board including the browser, so keep headroom). Exits non-zero
when a board is over budget, so a release can gate on it. Time spent in the browser
and on the network is not included: this is the server's share.
"""
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from datasets.explore import board_requests, run_explore
from datasets.management.commands.warm_boards import boards_reading


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))]


def _call(dataset, body, use_cache):
    try:
        run_explore(dataset, body, use_cache=use_cache)
        return None
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return str(exc).splitlines()[0][:160]
    finally:
        connections.close_all()      # each worker thread has its own metadata connection


def _call_inline(dataset, body, use_cache):
    try:
        run_explore(dataset, body, use_cache=use_cache)
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc).splitlines()[0][:160]


class Command(BaseCommand):
    help = "Time every board's server-side calls and compare the p95 with a budget."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", action="append", default=[])
        parser.add_argument("--dashboard", action="append", type=int, default=[])
        parser.add_argument("--budget-ms", type=int,
                            default=getattr(settings, "DIS_BOARD_BUDGET_MS", 3000))
        parser.add_argument("--runs", type=int, default=5)
        parser.add_argument("--concurrency", type=int, default=6,
                            help="Calls in flight per board (a browser: 6; 1 runs inline).")
        parser.add_argument("--cold", action="store_true",
                            help="Bypass the snapshot cache (the worst case after a promotion).")
        parser.add_argument("--json", default="", help="Also write the results to this file.")

    def handle(self, *args, **opts):
        boards = boards_reading(opts["datasource"], opts["dashboard"])
        if not boards:
            raise CommandError("No dashboard matches.")
        budget, use_cache = opts["budget_ms"], not opts["cold"]
        results, over = [], 0
        self.stdout.write(f"{'board':38s} {'calls':>5s} {'p50 ms':>7s} {'p95 ms':>7s} "
                          f"{'max ms':>7s}  budget {budget} ms ({'cold' if opts['cold'] else 'warm'})")
        workers = max(1, opts["concurrency"])
        pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        run_all = ((lambda calls: list(pool.map(lambda c: _call(c[0], c[1], use_cache), calls)))
                   if pool else
                   (lambda calls: [_call_inline(c[0], c[1], use_cache) for c in calls]))
        try:
            for board in boards:
                calls = board_requests(board)
                times, errors = [], []
                for _ in range(max(1, opts["runs"])):
                    t0 = time.perf_counter()
                    outcomes = run_all(calls)
                    times.append((time.perf_counter() - t0) * 1000)
                    errors += [e for e in outcomes if e]
                p95 = _p95(times)
                ok = p95 <= budget and not errors
                over += not ok
                results.append({"board": board.name, "id": board.id, "calls": len(calls),
                                "p50_ms": round(statistics.median(times), 1),
                                "p95_ms": round(p95, 1), "max_ms": round(max(times), 1),
                                "budget_ms": budget, "ok": ok, "errors": errors[:5],
                                "mode": "cold" if opts["cold"] else "warm"})
                self.stdout.write(f"{board.name[:38]:38s} {len(calls):5d} {statistics.median(times):7.0f} "
                                  f"{p95:7.0f} {max(times):7.0f}  {'OK' if ok else 'OVER'}"
                                  + (f"  {len(errors)} error(s)" if errors else ""))
        finally:
            if pool:
                pool.shutdown()
        if opts["json"]:
            with open(opts["json"], "w") as fh:
                json.dump(results, fh, indent=1)
        if over:
            raise CommandError(f"{over} board(s) over the {budget} ms budget or failing.")
        self.stdout.write(f"All {len(boards)} board(s) within {budget} ms.")
