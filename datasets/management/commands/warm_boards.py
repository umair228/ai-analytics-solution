"""Pre-compute every board's database calls into the snapshot cache (P10).

    manage.py warm_boards --datasource "PMSv2 Reporting" [--datasource ...]
    manage.py warm_boards --dashboard 3

Run right after a promotion (the nightly hook does): the new restore_id makes every
cached result a miss, so without a warm-up the first manager of the morning waits for
the slowest board. It replays exactly the requests the dashboard UI makes
(datasets.explore.widget_explore_bodies) through the same code path, so the cache it
fills is the one the browser reads. Needs a shared cache (DIS_CACHE=db) to help other
processes. Exits non-zero if any call failed; the boards still work uncached.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from dashboards.models import Dashboard
from datasets.explore import board_requests, run_explore


def boards_reading(names, ids):
    qs = Dashboard.objects.all().order_by("id")
    if ids:
        qs = qs.filter(id__in=ids)
    if names:
        qs = qs.filter(widgets__dataset__query__datasource__name__in=names).distinct()
    return list(qs)


class Command(BaseCommand):
    help = "Fill the snapshot cache with every board's server-side calls."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", action="append", default=[],
                            help="Only boards with a widget reading this source (repeatable).")
        parser.add_argument("--dashboard", action="append", type=int, default=[],
                            help="Only this dashboard id (repeatable).")

    def handle(self, *args, **opts):
        boards = boards_reading(opts["datasource"], opts["dashboard"])
        if not boards:
            raise CommandError("No dashboard matches.")
        calls = failures = 0
        t_all = time.monotonic()
        for board in boards:
            t0, n, bad = time.monotonic(), 0, 0
            for dataset, body in board_requests(board):
                n += 1
                try:
                    run_explore(dataset, body)
                except Exception as exc:  # noqa: BLE001 — one widget must not stop the rest
                    bad += 1
                    self.stderr.write(f"  {board.name} · {dataset.name}: "
                                      f"{str(exc).splitlines()[0][:160]}")
            calls, failures = calls + n, failures + bad
            self.stdout.write(f"{'OK ' if not bad else 'ERR'} {board.name}: {n} call(s) "
                              f"in {time.monotonic() - t0:.2f} s" + (f", {bad} failed" if bad else ""))
        self.stdout.write(f"Warmed {len(boards)} board(s), {calls} call(s), {failures} failed, "
                          f"{time.monotonic() - t_all:.1f} s.")
        if failures:
            raise CommandError(f"{failures} call(s) failed while warming.")
