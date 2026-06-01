"""
Instrument downtime forecast.

Ported from Portal-BE/timeseriesforecast/views.py. Reads ON/OFF event logs from
dbo.INSTRUMENTS1_LOG, derives monthly downtime hours per instrument, and serves
a NeuralProphet forecast (loading a cached per-instrument model when present,
otherwise training and caching one).

    GET /api/downtime-forecast/?instrument=05_C_401&history_months=24
        &forecast_months=6&outlier_cap=120
"""
import os
import pickle

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import pandas as pd
import numpy as np

from ..db import get_connection, read_sql, tbl

APP_DIR = os.path.dirname(os.path.dirname(__file__))
MODELS_DIR = os.path.join(APP_DIR, "artifacts", "downtime")
os.makedirs(MODELS_DIR, exist_ok=True)

# Default instrument shown when none is selected (refinery GC).
DEFAULT_INSTRUMENT = "GC-401"


def _fallback_payload(instrument=DEFAULT_INSTRUMENT, reason="database connection issues"):
    """Static payload returned when the DB or model pipeline is unavailable."""
    return {
        "instrument": instrument,
        "historical": {
            "labels": ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"],
            "values": [20, 25, 30, 35, 40, 45],
            "label": "Actual Downtime (Hours) - Fallback Data",
            "uptime_percentage": [97.2, 96.5, 95.8, 95.1, 94.4, 93.8],
        },
        "forecast": {
            "labels": ["2024-07", "2024-08", "2024-09", "2024-10", "2024-11", "2024-12"],
            "values": [50, 55, 60, 65, 70, 75],
            "label": "Forecasted Downtime (Hours) - Fallback Data",
            "uptime_percentage": [93.1, 92.4, 91.7, 91.0, 90.3, 89.6],
        },
        "control_limit": {
            "labels": ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06",
                       "2024-07", "2024-08", "2024-09", "2024-10", "2024-11", "2024-12"],
            "values": [60] * 12,
            "label": "Acceptable Limit (Hours)",
        },
        "trend": {
            "historical": {"slope": 5.0, "direction": "increasing"},
            "forecast": {"slope": 5.0, "direction": "increasing"},
        },
        "stats": {
            "average_forecast": 62.5,
            "max_forecast_month": "2024-12",
            "max_forecast_value": 75,
            "min_forecast_month": "2024-07",
            "min_forecast_value": 50,
        },
        "insights": (
            f"For instrument {instrument} (using fallback data due to {reason}):\n"
            "- Historically, the trend has been increasing with a slope of 5.0.\n"
            "- The forecast suggests an increasing trend ahead, with a slope of 5.0.\n"
            "- On average, uptime in the past has been around 95.0%.\n"
            "- Looking ahead, uptime is expected to remain around 91.0%.\n"
            "- Forecasted average downtime per month is about 62.5 hours.\n"
            "- The highest downtime is expected in 2024-12 at approximately 75 hours.\n"
            "- The lowest downtime is expected in 2024-07 at approximately 50 hours.\n"
            "- The acceptable downtime limit remains 60 hours per month."
        ),
    }


def _load_or_train(model_path, df_prophet):
    """Load a cached NeuralProphet model, retraining if the file is missing or
    cannot be unpickled (e.g. a library-version mismatch)."""
    from neuralprophet import NeuralProphet
    from ..ml import load_model

    if os.path.exists(model_path):
        try:
            return load_model(model_path)
        except Exception as exc:  # corrupt / incompatible pickle -> retrain
            print(f"[downtime] could not load {model_path} ({exc}); retraining")

    model = NeuralProphet(
        yearly_seasonality=True,
        weekly_seasonality=False,
        daily_seasonality=False,
        learning_rate=0.01,
    )
    model.fit(df_prophet, freq="MS")
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    return model


class DowntimeForecastView(APIView):
    def get(self, request):
        selected_instrument = request.query_params.get("instrument", DEFAULT_INSTRUMENT)
        try:
            try:
                conn = get_connection()
            except Exception as e:
                print(f"Database connection failed: {e}")
                return Response(_fallback_payload(selected_instrument), status=status.HTTP_200_OK)

            query = f"""
            SELECT INSTRUMENT, EVENT_TYPE, ENTERED_BY, ENTERED_ON
            FROM {tbl('INSTRUMENTS1_LOG')}
            WHERE EVENT_TYPE IN ('ON', 'OFF') AND ENTERED_ON IS NOT NULL
            ORDER BY INSTRUMENT, ENTERED_ON;
            """
            df = read_sql(query, conn, parse_dates=["ENTERED_ON"])
            conn.close()

            # --- Preprocessing: per-instrument durations between events ---
            df["ENTERED_ON"] = pd.to_datetime(df["ENTERED_ON"])
            df = df.sort_values(by=["INSTRUMENT", "ENTERED_ON"])
            df["DURATION"] = df.groupby("INSTRUMENT")["ENTERED_ON"].diff().shift(-1)
            df["DURATION"] = df["DURATION"].dt.total_seconds()
            df = df[df["DURATION"].notna()]
            df["YearMonth"] = df["ENTERED_ON"].dt.to_period("M").dt.to_timestamp()

            model_path = os.path.join(MODELS_DIR, f"DowntimeModel_{selected_instrument}.pkl")

            off_df = (
                df[(df["EVENT_TYPE"] == "OFF") & (df["INSTRUMENT"] == selected_instrument)]
                .groupby(["YearMonth"])["DURATION"]
                .sum()
                .reset_index()
            )
            off_df["DURATION"] = off_df["DURATION"] / 3600
            df_prophet = off_df.rename(columns={"YearMonth": "ds", "DURATION": "y"})
            df_prophet = df_prophet.sort_values(by="ds")

            model = _load_or_train(model_path, df_prophet)

            # --- Query params for windowing / outlier filtering ---
            history_months = int(request.query_params.get("history_months", 24))
            forecast_months = int(request.query_params.get("forecast_months", 6))
            outlier_cap = float(request.query_params.get("outlier_cap", 120))  # hours

            df_prophet = df_prophet.sort_values(by="ds")
            if len(df_prophet) > history_months:
                df_prophet = df_prophet.iloc[-history_months:]
            df_prophet["y"] = np.clip(df_prophet["y"], 0, outlier_cap)

            # --- Forecast ---
            future = model.make_future_dataframe(df_prophet, periods=forecast_months)
            forecast = model.predict(future).sort_values(by="ds")

            last_date = df_prophet["ds"].max()
            forecast_future = forecast[forecast["ds"] > last_date].head(forecast_months)

            hist_labels = df_prophet["ds"].dt.strftime("%Y-%m").tolist()
            hist_values = df_prophet["y"].round(2).tolist()

            if hist_values and hist_values[-1] < 40:
                hist_labels = hist_labels[:-1]
                hist_values = hist_values[:-1]
                df_prophet = df_prophet.iloc[:-1]

            hist_uptime_percentage = [round((720 - v) / 720 * 100, 2) for v in hist_values]

            forecast_labels = forecast_future["ds"].dt.strftime("%Y-%m").tolist()
            forecast_values = [round(float(v)) for v in forecast_future["yhat1"].tolist()]
            forecast_uptime_percentage = [round((720 - v) / 720 * 100, 2) for v in forecast_values]

            def _slope(vals):
                # np.polyfit needs >= 2 points; treat sparse series as flat.
                if len(vals) < 2:
                    return 0.0
                return float(np.polyfit(np.arange(len(vals)), np.array(vals), 1)[0])

            slope_hist = _slope(hist_values)
            slope_fore = _slope(forecast_values)

            trend_hist = "increasing" if slope_hist > 0 else "decreasing" if slope_hist < 0 else "stable"
            trend_fore = "increasing" if slope_fore > 0 else "decreasing" if slope_fore < 0 else "stable"

            avg_forecast = round(float(np.mean(forecast_values)), 2)
            max_i = int(np.argmax(forecast_values))
            min_i = int(np.argmin(forecast_values))

            control_limit = 60
            control_limit_labels = hist_labels + forecast_labels
            control_limit_values = [control_limit] * len(control_limit_labels)

            insights = (
                f"For instrument {selected_instrument}:\n"
                f"- Historically, the trend has been {trend_hist} with a slope of {round(float(slope_hist), 2)}.\n"
                f"- The forecast suggests a {trend_fore} trend ahead, with a slope of {round(float(slope_fore), 2)}.\n"
                f"- On average, uptime in the past has been around {round(np.mean(hist_uptime_percentage), 2)}%.\n"
                f"- Looking ahead, uptime is expected to remain around {round(np.mean(forecast_uptime_percentage), 2)}%.\n"
                f"- Forecasted average downtime per month is about {avg_forecast} hours.\n"
                f"- The highest downtime is expected in {forecast_labels[max_i]} at approximately {forecast_values[max_i]} hours.\n"
                f"- The lowest downtime is expected in {forecast_labels[min_i]} at approximately {forecast_values[min_i]} hours.\n"
                f"- The acceptable downtime limit remains {control_limit} hours per month."
            )

            return Response({
                "instrument": selected_instrument,
                "historical": {
                    "labels": hist_labels,
                    "values": hist_values,
                    "label": "Actual Downtime (Hours)",
                    "uptime_percentage": hist_uptime_percentage,
                },
                "forecast": {
                    "labels": forecast_labels,
                    "values": forecast_values,
                    "label": "Forecasted Downtime (Hours)",
                    "uptime_percentage": forecast_uptime_percentage,
                },
                "control_limit": {
                    "labels": control_limit_labels,
                    "values": control_limit_values,
                    "label": "Acceptable Limit (Hours)",
                },
                "trend": {
                    "historical": {"slope": round(float(slope_hist), 2), "direction": trend_hist},
                    "forecast": {"slope": round(float(slope_fore), 2), "direction": trend_fore},
                },
                "stats": {
                    "average_forecast": avg_forecast,
                    "max_forecast_month": forecast_labels[max_i],
                    "max_forecast_value": forecast_values[max_i],
                    "min_forecast_month": forecast_labels[min_i],
                    "min_forecast_value": forecast_values[min_i],
                },
                "insights": insights,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"Error in DowntimeForecastView: {e}")
            payload = _fallback_payload(selected_instrument, reason="an error")
            payload["historical"]["label"] = "Actual Downtime (Hours) - Error (Using Fallback Data)"
            payload["forecast"]["label"] = "Forecasted Downtime (Hours) - Error (Using Fallback Data)"
            return Response(payload, status=status.HTTP_200_OK)
