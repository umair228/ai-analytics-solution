"""
Inventory stock-consumption forecast.

Ported from Portal-BE/inventoryApp/views/inventory.py. Reads PULL transactions
joined to inventory items and serves monthly consumption forecasts for the
allowed stocks (Methanol / Ethanol).

    GET /api/inventory-forecast/?stock=METHANOL&months=6&pastStartYear=2021&pastEndYear=2025

Note: in some LIMS databases the stock is named with a prefix (e.g. FD_METHANOL,
ENV_ETHANOL). Matching is suffix-aware so those rows resolve to METHANOL/ETHANOL.
"""
import os
import pickle
import datetime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import pandas as pd
import numpy as np

from ..db import get_inventory_connection, read_sql

APP_DIR = os.path.dirname(os.path.dirname(__file__))
MODEL_DIR = os.path.join(APP_DIR, "artifacts", "inventory")
os.makedirs(MODEL_DIR, exist_ok=True)
modelPathInventory = os.path.join(MODEL_DIR, "InventoryModel_{stock}.pkl")

ALLOWED_STOCKS = {"METHANOL", "ETHANOL"}


def _normalize_stock(raw: str) -> str:
    """Map a raw STOCK string to an allowed canonical stock, suffix-aware.

    Handles bare 'METHANOL'/'ETHANOL' and prefixed forms like 'FD_METHANOL',
    'ENV_ETHANOL'. METHANOL is checked first because 'METHANOL' ends with
    'ETHANOL' as a substring.
    """
    name = str(raw).upper().strip()
    if name.endswith("METHANOL"):
        return "METHANOL"
    if name.endswith("ETHANOL"):
        return "ETHANOL"
    return name


def getInventoryData():
    sql = """
    SELECT
        it.ITEM_NUMBER,
        it.STOCK,
        it.INVENTORY_TYPE,
        tr.TRANSACTION_DATE,
        tr.TRANSACTION_TYPE,
        tr.TRANSACTION_STATUS,
        tr.QUANTITY,
        tr.UNITS
    FROM dbo.INVENTORY_TRANS AS tr
    INNER JOIN dbo.INVENTORY_ITEM AS it
        ON tr.INVENTORY_ITEM = it.ITEM_NUMBER
    WHERE
        tr.TRANSACTION_DATE IS NOT NULL
        AND tr.TRANSACTION_TYPE = 'PULL'
        AND tr.TRANSACTION_STATUS = 'C'
        AND tr.QUANTITY IS NOT NULL
    """
    with get_inventory_connection() as conn:
        df = read_sql(sql, conn, parse_dates=["TRANSACTION_DATE"])

    df["inventoryName"] = df["STOCK"].apply(_normalize_stock)
    df = df[df["inventoryName"].isin(ALLOWED_STOCKS)].copy()
    df = df[df["QUANTITY"] > 0].copy()
    df["ds"] = df["TRANSACTION_DATE"]
    df["y"] = df["QUANTITY"].astype(float)
    return df


def trainModel(df, modelPath):
    from neuralprophet import NeuralProphet

    df = df.copy()
    df["ds"] = df["ds"].dt.to_period("M").dt.to_timestamp()
    df = df.groupby("ds")["y"].sum().reset_index()
    df = df[(df["ds"].dt.year >= 2010) & (df["ds"].dt.year <= 2025)]

    if not df.empty:
        q1 = df["y"].quantile(0.25)
        q3 = df["y"].quantile(0.75)
        iqr = q3 - q1
        upperBound = q3 + 1.5 * iqr
        df["y"] = df["y"].clip(upper=upperBound)

        m = NeuralProphet(
            growth="off",
            yearly_seasonality=True,
            weekly_seasonality=False,
            daily_seasonality=False,
            seasonality_mode="multiplicative",
            trend_reg=0.1,
            seasonality_reg=0.5,
            n_changepoints=10,
            epochs=1000,
        )
        m.fit(df, freq="M")
    else:
        m = NeuralProphet()

    with open(modelPath, "wb") as f:
        pickle.dump(m, f)
    return m


class InventoryForecast(APIView):
    def get(self, request):
        stock = request.query_params.get("stock")
        months = int(request.query_params.get("months", 6))

        if not stock:
            return Response({"error": "Missing 'stock' query parameter."}, status=status.HTTP_400_BAD_REQUEST)
        stock = stock.upper().strip()
        if stock not in ALLOWED_STOCKS:
            return Response({"error": f"Unsupported stock '{stock}'. Allowed: {sorted(ALLOWED_STOCKS)}"}, status=status.HTTP_400_BAD_REQUEST)

        df = getInventoryData()
        if stock not in df["inventoryName"].unique():
            return Response({"error": f"No data found for stock '{stock}'."}, status=status.HTTP_400_BAD_REQUEST)

        df = df[df["inventoryName"] == stock].copy()
        if df.empty or df["ds"].nunique() < 30:
            return Response({"error": "Need ≥30 days of data."}, status=status.HTTP_400_BAD_REQUEST)

        df["ds"] = df["ds"].dt.to_period("M").dt.to_timestamp()

        modelDf = df[(df["ds"].dt.year >= 2010) & (df["ds"].dt.year <= 2025)]
        modelDf = modelDf.groupby("ds")["y"].sum().reset_index()
        floor = modelDf["y"].min() if not modelDf.empty else 0.0
        modelPath = modelPathInventory.format(stock=stock)

        if os.path.exists(modelPath):
            try:
                from ..ml import load_model
                m = load_model(modelPath)
            except Exception as e:
                print(f"Error loading inventory model {modelPath}: {e}; retraining")
                if modelDf.empty:
                    return Response({"error": "No training data."}, status=status.HTTP_400_BAD_REQUEST)
                m = trainModel(modelDf.copy(), modelPath)
        else:
            if modelDf.empty:
                return Response({"error": "No training data."}, status=status.HTTP_400_BAD_REQUEST)
            m = trainModel(modelDf.copy(), modelPath)

        now = datetime.datetime.now()
        startMonth = datetime.datetime(now.year, now.month, 1)
        endDate = now + pd.DateOffset(months=months) - pd.DateOffset(days=1)
        endMonth = datetime.datetime(endDate.year, endDate.month, 1)
        lastTrain = modelDf["ds"].max()
        totalMonths = (endMonth.year - lastTrain.year) * 12 + (endMonth.month - lastTrain.month) + 1

        future = m.make_future_dataframe(modelDf, periods=totalMonths, n_historic_predictions=False)
        forecast = m.predict(future)
        forecast["ds"] = pd.to_datetime(forecast["ds"])

        window = forecast[(forecast["ds"] >= startMonth) & (forecast["ds"] <= endMonth)].copy()
        if window.empty:
            return Response({"error": "No forecast points in the requested window. Check training horizon."}, status=status.HTTP_400_BAD_REQUEST)

        labels = window["ds"].dt.strftime("%b %Y").tolist()
        values = np.clip(window["yhat1"], floor, None).round().astype(int).tolist()
        avg = round(float(np.mean(values)), 2)
        maxI, minI = int(np.argmax(values)), int(np.argmin(values))

        pastStartRaw = request.query_params.get("pastStartYear")
        pastEndRaw = request.query_params.get("pastEndYear")
        if not pastStartRaw or not pastEndRaw:
            return Response({"error": "Both 'pastStartYear' and 'pastEndYear' must be provided."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pastStartYear = int(pastStartRaw)
            pastEndYear = int(pastEndRaw)
        except Exception:
            return Response({"error": "Invalid year format for 'pastStartYear' or 'pastEndYear'."}, status=status.HTTP_400_BAD_REQUEST)
        if pastEndYear < pastStartYear:
            return Response({"error": "Invalid past data range: pastEndYear must be greater than or equal to pastStartYear."}, status=status.HTTP_400_BAD_REQUEST)

        dfYearsAvailable = set(df["ds"].dt.year.unique().tolist())
        requestedYears = set(range(pastStartYear, pastEndYear + 1))
        missingYears = sorted(list(requestedYears - dfYearsAvailable))
        if missingYears:
            missingStr = ", ".join(str(y) for y in missingYears)
            return Response({"error": f"Past data missing for year(s): {missingStr}. Please choose a different range."}, status=status.HTTP_400_BAD_REQUEST)

        past = df[(df["ds"].dt.year >= pastStartYear) & (df["ds"].dt.year <= pastEndYear)].copy()
        past["monthTs"] = past["ds"]
        pastMonthly = past.groupby("monthTs")["y"].sum().reset_index()
        if not pastMonthly.empty:
            pastMonthly["date"] = pastMonthly["monthTs"].dt.strftime("%b %Y")
            pastMonthly["samples"] = pastMonthly["y"].astype(int)
            pastData = pastMonthly[["date", "samples"]].to_dict(orient="records")
            pastY = pastMonthly["samples"].values
            pastUpperQuartile = int(np.percentile(pastY, 75))
            pastLowerQuartile = int(np.percentile(pastY, 25))
        else:
            pastData, pastUpperQuartile, pastLowerQuartile = [], 0, 0

        forecastUpperQuartile = int(np.percentile(values, 75))
        forecastLowerQuartile = int(np.percentile(values, 25))

        return Response({
            "stock": stock,
            "labels": labels,
            "values": values,
            "label": f"Predicted Monthly Consumption ({stock})",
            "orangeDashedLineAverage(Y-Axis)": avg,
            "orangeLine(X-Axis)": {"startDate": labels[0], "endDate": labels[-1]},
            "highestMonth(Stats)": {"date": labels[maxI], "samples": values[maxI]},
            "lowestMonth(Stats)": {"date": labels[minI], "samples": values[minI]},
            "storyboard": (
                f"Based on historical usage from 2010 to 2025, the model forecasts consumption "
                f"from {labels[0]} to {labels[-1]}. The highest predicted month is {labels[maxI]} "
                f"({values[maxI]} units), and the lowest is {labels[minI]} ({values[minI]} units)."
            ),
            "pastData": pastData,
            "UpperQuartile-Forecasted": forecastUpperQuartile,
            "LowerQuartile-Forecasted": forecastLowerQuartile,
            "UpperQuartile-pastData": pastUpperQuartile,
            "LowerQuartile-pastData": pastLowerQuartile,
        }, status=status.HTTP_200_OK)
