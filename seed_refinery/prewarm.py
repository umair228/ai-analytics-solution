"""
Pre-warm (train + cache) and verify the forecasting endpoints against the
refinery SQLite warehouse, so the demo's first clicks are instant.

    .venv/bin/python -m seed_refinery.prewarm            # all features
    .venv/bin/python -m seed_refinery.prewarm downtime sample   # subset
"""
import os
import sys
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

from accounts.models import User  # noqa: E402
from forecasting.views import (  # noqa: E402
    DowntimeForecastView, InventoryForecast, SampleForecast, SectionForecast,
)
from forecasting.views.sample import labs  # noqa: E402

admin = User.objects.filter(role="admin").first() or User.objects.first()
factory = APIRequestFactory()


def call(label, view, path, params):
    req = factory.get(path, params)
    force_authenticate(req, user=admin)
    t = time.time()
    try:
        resp = view.as_view()(req)
        dt = time.time() - t
        data = resp.data if hasattr(resp, "data") else {}
        err = isinstance(data, dict) and (data.get("error") or data.get("warning"))
        n = len(data.get("values", [])) if isinstance(data, dict) else 0
        ok = resp.status_code == 200 and not err
        flag = "OK " if ok else "ERR"
        detail = "" if ok else f" :: {str(err or data)[:160]}"
        print(f"  [{flag}] {label:30s} {dt:6.1f}s  status={resp.status_code} points={n}{detail}")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] {label:30s} {time.time()-t:6.1f}s  EXCEPTION: {exc}")
        return False


def main():
    want = set(a.lower() for a in sys.argv[1:]) or {"downtime", "sample", "section", "inventory"}
    print(f"Pre-warming forecasts (user={admin.username}) features={sorted(want)}")
    t0 = time.time()

    if "downtime" in want:
        for inst in ["GC-401", "ADU-05", "CFR-OCTANE-01"]:
            call(f"downtime {inst}", DowntimeForecastView, "/api/downtime-forecast/",
                 {"instrument": inst, "history_months": 24, "forecast_months": 6})

    if "sample" in want:
        call("sample (all sections)", SampleForecast, "/api/sample-forecast/",
             {"months": 6, "pastStartYear": 2021, "pastEndYear": 2023})

    if "section" in want:
        for lab in labs:
            call(f"section {lab}", SectionForecast, "/api/section-forecast/",
                 {"lab": lab, "months": 6, "pastStartYear": 2021, "pastEndYear": 2023})

    if "inventory" in want:
        for stock in ["METHANOL", "ETHANOL"]:
            call(f"inventory {stock}", InventoryForecast, "/api/inventory-forecast/",
                 {"stock": stock, "months": 6, "pastStartYear": 2021, "pastEndYear": 2025})

    print(f"Done in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
