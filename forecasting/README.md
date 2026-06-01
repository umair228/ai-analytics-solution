# Forecasting app (LIMS time-series)

Ports the three Portal-BE forecasting features into the DSE backend. All views
read operational LIMS tables directly over **pyodbc** (no Django ORM) and serve
**NeuralProphet** forecasts, loading cached `.pkl` models when present.

## Endpoints (mounted under `/api/`)

| Endpoint | Source table(s) | Key query params |
|---|---|---|
| `GET /api/downtime-forecast/`  | `dbo.INSTRUMENTS1_LOG` | `instrument`, `history_months`, `forecast_months`, `outlier_cap` |
| `GET /api/sample-forecast/`    | `dbo.SAMPLE` | `months`, `pastStartYear`, `pastEndYear` |
| `GET /api/section-forecast/`   | `dbo.SAMPLE` | `lab`, `months`, `pastStartYear`, `pastEndYear` |
| `GET /api/inventory-forecast/` | `dbo.INVENTORY_TRANS` + `dbo.INVENTORY_ITEM` | `stock`, `months`, `pastStartYear`, `pastEndYear` |

All require JWT auth (platform default). Examples:

```
/api/downtime-forecast/?instrument=05_C_401&history_months=24&forecast_months=6&outlier_cap=120
/api/sample-forecast/?months=6&pastStartYear=2021&pastEndYear=2023
/api/section-forecast/?lab=Food&months=6&pastStartYear=2021&pastEndYear=2023
/api/inventory-forecast/?stock=METHANOL&months=6&pastStartYear=2021&pastEndYear=2025
```

## Configuration (`.env`)

```
DB_ENGINE=mssql
DB_NAME=SMJMUN_DEV
DB_USER=SA
DB_PASSWORD=...
DB_HOST=127.0.0.1
DB_PORT=1433
DB_DRIVER=ODBC Driver 18 for SQL Server
# optional inventory overrides: DB_HOST_INVENTORY, DB_INVENTORY_NAME
FORECAST_SYNC_LABS_COLUMN=False   # back-fill dbo.SAMPLE.Labs on each request (off)
FORECAST_DB_TIMEOUT=15
```

Requires the **Microsoft ODBC Driver 18 for SQL Server** on the host.

## Dependencies / pins (see requirements.txt)

NeuralProphet 0.9.0 pulls torch + pytorch-lightning. Two pins matter:

- `pandas>=2.2,<2.3` — NeuralProphet 0.9.0 calls `Series.view()`, removed in pandas 3.0.
- `pytorch-lightning>=2.2,<2.5` — lightning ≥2.5.2 adds `_pending_litmodels_tip`, which
  the 0.9.0 callback path doesn't expect.

NeuralProphet also holds `numpy<2` (1.26.x). These match the Portal-BE stack.

## Pre-trained models

Cached models live in `artifacts/{downtime,sample,inventory}/`. They were stored
in Git LFS in Portal-BE (`git lfs pull` to fetch the real binaries). On load they
are passed through `ml.sanitize_model`, which rebuilds the PyTorch-Lightning
logger against a writable runtime dir (the pickles carry the training machine's
`save_dir`). If a model is missing or fails to unpickle, the view **retrains**
from the live data and caches the result.

## Notes

- **Inventory data gap:** in the current `SMJMUN_DEV` extract, methanol/ethanol
  stocks are named `FD_METHANOL` / `ENV_ETHANOL` and have <30 days of PULL
  transactions, so `inventory-forecast` returns *"Need ≥30 days of data."* The
  stock matcher is suffix-aware (`_normalize_stock`) so it resolves those names;
  the endpoint produces a forecast as soon as a DB with sufficient history is set.
