"""
Seed synthetic PULL transactions for METHANOL / ETHANOL so the inventory
forecast has enough history to model, then (optionally) retrain the models.

The LIMS extract in SMJMUN_DEV has only a handful of methanol/ethanol PULL
transactions (< 30 days), so /api/inventory-forecast/ reports "insufficient
data". This command inserts realistic monthly consumption transactions across
a configurable year range. Rows are tagged with TRANSACTION_COMMENT='SEED_AI_FORECAST'
so they are easy to identify and the command is idempotent (it deletes prior
seed rows before inserting).

    python manage.py seed_inventory                      # 2010-2025, 3 pulls/month, no retrain
    python manage.py seed_inventory --retrain            # also retrain METHANOL/ETHANOL models
    python manage.py seed_inventory --clear              # remove seed rows and exit
    python manage.py seed_inventory --start-year 2015 --end-year 2025 --per-month 4 --retrain
"""
import os
import math
import uuid
import random
import datetime

from django.core.management.base import BaseCommand

from forecasting.db import get_inventory_connection

SEED_MARKER = "SEED_AI_FORECAST"

# STOCK names that normalize to each canonical stock already exist in
# dbo.INVENTORY_ITEM (FD_METHANOL=8, FD_ETHANOL=9). Use those item numbers.
STOCK_ITEMS = {"METHANOL": 8, "ETHANOL": 9}
# Rough monthly consumption profile per stock (units/month before seasonality).
STOCK_BASE = {"METHANOL": 180, "ETHANOL": 140}


class Command(BaseCommand):
    help = "Seed synthetic METHANOL/ETHANOL PULL transactions for inventory forecasting."

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int, default=2010)
        parser.add_argument("--end-year", type=int, default=2025)
        parser.add_argument("--per-month", type=int, default=3)
        parser.add_argument("--units", default="ML")
        parser.add_argument("--retrain", action="store_true",
                            help="Delete cached models and retrain on the seeded data.")
        parser.add_argument("--clear", action="store_true",
                            help="Delete previously seeded rows and exit.")

    def handle(self, *args, **opts):
        conn = get_inventory_connection(autocommit=True)
        cur = conn.cursor()

        deleted = cur.execute(
            "DELETE FROM dbo.INVENTORY_TRANS WHERE TRANSACTION_COMMENT = ?", (SEED_MARKER,)
        ).rowcount
        self.stdout.write(f"Removed {deleted} existing seed rows.")
        if opts["clear"]:
            conn.close()
            self.stdout.write(self.style.SUCCESS("Cleared seed data."))
            return

        rnd = random.Random(42)  # deterministic
        start, end, per_month = opts["start_year"], opts["end_year"], max(1, opts["per_month"])
        units = opts["units"]
        total = 0

        for stock, item in STOCK_ITEMS.items():
            base = STOCK_BASE[stock]
            for year in range(start, end + 1):
                trend = (year - start) * 6  # gentle upward trend year over year
                for month in range(1, 13):
                    seasonal = 50 * math.sin(2 * math.pi * (month - 1) / 12)
                    monthly_total = max(40.0, base + trend + seasonal + rnd.uniform(-25, 25))
                    days = sorted(rnd.sample(range(1, 27), per_month))
                    for day in days:
                        qty = max(1.0, round(monthly_total / per_month * rnd.uniform(0.7, 1.3), 1))
                        ts = datetime.datetime(year, month, day,
                                               rnd.randint(8, 16), rnd.randint(0, 59), rnd.randint(0, 59))
                        code = "{" + str(uuid.uuid4()).upper() + "}"
                        cur.execute(
                            """
                            INSERT INTO dbo.INVENTORY_TRANS
                                (ENTRY_CODE, INVENTORY_ITEM, TRANSACTION_TYPE, TRANSACTION_STATUS,
                                 TRANSACTION_DATE, PLANNED_DATE, TRANSACTION_BY, DESCRIPTION,
                                 TRANSACTION_COMMENT, QUANTITY, UNITS, ORDER_NUMBER)
                            VALUES (?, ?, 'PULL', 'C', ?, ?, 'AI_SEED',
                                    'Synthetic seed for forecasting demo', ?, ?, ?, 1)
                            """,
                            (code, item, ts, ts, SEED_MARKER, qty, units),
                        )
                        total += 1
            self.stdout.write(f"  seeded {stock} (item {item}) for {start}-{end}")

        self.stdout.write(self.style.SUCCESS(
            f"Inserted {total} PULL transactions tagged '{SEED_MARKER}'."
        ))
        conn.close()

        if opts["retrain"]:
            self._retrain()

    def _retrain(self):
        from forecasting.views.inventory import modelPathInventory, getInventoryData, trainModel

        self.stdout.write("Retraining inventory models on seeded data (this can take a minute)…")
        for stock in STOCK_ITEMS:
            path = modelPathInventory.format(stock=stock)
            if os.path.exists(path):
                os.remove(path)

        df = getInventoryData()
        for stock in STOCK_ITEMS:
            sdf = df[df["inventoryName"] == stock][["ds", "y"]].copy()
            if sdf.empty:
                self.stdout.write(self.style.WARNING(f"  no data for {stock}, skipped"))
                continue
            trainModel(sdf, modelPathInventory.format(stock=stock))
            self.stdout.write(f"  retrained {stock} model ({sdf['ds'].dt.to_period('M').nunique()} months)")
        self.stdout.write(self.style.SUCCESS("Retrained METHANOL/ETHANOL models."))
