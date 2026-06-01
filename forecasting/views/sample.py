"""
Lab sample-volume forecast (all labs + per lab).

Ported from Portal-BE/SharjahAnalytics/views/TimeSeriesSample.py. Reads sample
login dates and GROUP_NAME from dbo.SAMPLE, classifies each group into a lab,
and serves monthly sample-count forecasts.

    GET /api/sample-forecast/?months=6&pastStartYear=2021&pastEndYear=2023
    GET /api/section-forecast/?lab=Food&months=6&pastStartYear=2021&pastEndYear=2023
"""
import os
import pickle
import time
import datetime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import pandas as pd
import numpy as np
from django.conf import settings

from ..db import date_cast, get_connection, is_sqlite, read_sql, tbl

# refinery laboratory sections that get a per-section sample-volume model.
labs = [
    "Crude & Distillation", "Gasoline/Mogas", "Naphtha & Aromatics",
    "Middle Distillates", "Fuel Oil & Asphalt", "Gas & LPG",
    "Environmental & Water",
]

# LabWare GROUP_NAME -> refinery section. (Reverting to the Sharjah municipality
# LIMS needs the old labs list + if/else mapping — preserved in git history.)
GROUP_TO_SECTION = {
    "CRUDE_DIST": "Crude & Distillation", "CRUDE_ASSAY": "Crude & Distillation",
    "ATMOS_DIST": "Crude & Distillation",
    "MOGAS_CHEM": "Gasoline/Mogas", "MOGAS_PHYS": "Gasoline/Mogas",
    "GASOLINE_OCT": "Gasoline/Mogas",
    "NAPH_AROM": "Naphtha & Aromatics", "REFORMATE": "Naphtha & Aromatics",
    "BTX_AROM": "Naphtha & Aromatics", "PARA_XYL": "Naphtha & Aromatics",
    "JET_KERO": "Middle Distillates", "DIESEL_PHYS": "Middle Distillates",
    "GASOIL_HYDRO": "Middle Distillates", "MIDDIST": "Middle Distillates",
    "FUELOIL_HSFO": "Fuel Oil & Asphalt", "ASPHALT_BIT": "Fuel Oil & Asphalt",
    "LUBE_BASEOIL": "Fuel Oil & Asphalt", "LUBE_ASPHALT": "Fuel Oil & Asphalt",
    "GAS_LPG": "Gas & LPG", "REFGAS_GC": "Gas & LPG", "LPG_SPEC": "Gas & LPG",
    "ENV_WATER": "Environmental & Water", "UTIL_BFW": "Environmental & Water",
    "EFFLUENT": "Environmental & Water", "ENV_AIR": "Environmental & Water",
}

APP_DIR = os.path.dirname(os.path.dirname(__file__))
MODEL_DIR = os.path.join(APP_DIR, "artifacts", "sample")
os.makedirs(MODEL_DIR, exist_ok=True)
modelPathCombined = os.path.join(MODEL_DIR, "RefinerySampleCombined.pkl")
modelPathLabType = os.path.join(MODEL_DIR, "RefinerySampleSection_{lab}.pkl")


def classifyLab(group):
    return GROUP_TO_SECTION.get(group, "Other")


def ensureLabsColumnExists():
    if is_sqlite():
        return  # SQLite warehouse already ships the classification in-memory.
    try:
        with get_connection(autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'SAMPLE' AND COLUMN_NAME = 'Labs'
                """
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute("ALTER TABLE dbo.SAMPLE ADD Labs NVARCHAR(100);")
                conn.commit()
    except Exception as e:
        print(f"Error ensuring Labs column exists: {e}")


def getLabData():
    max_retries = 2
    retry_delay = 1
    for attempt in range(max_retries):
        try:
            ensureLabsColumnExists()

            sql = f"""
            SELECT
                {date_cast('LOGIN_DATE')} AS ds,
                GROUP_NAME
            FROM {tbl('SAMPLE')}
            WHERE LOGIN_DATE IS NOT NULL AND GROUP_NAME IS NOT NULL
            """
            with get_connection(autocommit=True) as conn:
                df = read_sql(sql, conn, parse_dates=["ds"])
                if df.empty:
                    print("Warning: No data returned from database query")
                    return pd.DataFrame(columns=["ds", "Labs", "y"])

                df["Labs"] = df["GROUP_NAME"].apply(classifyLab)

                # Optional: back-fill the Labs column on the source table.
                # Off by default (FORECAST_SYNC_LABS_COLUMN) since the forecast
                # uses the in-memory classification and the write is a side-effect.
                if getattr(settings, "FORECAST_SYNC_LABS_COLUMN", False) and not is_sqlite():
                    cursor = conn.cursor()
                    for groupName, lab in df[["GROUP_NAME", "Labs"]].drop_duplicates().values:
                        cursor.execute(
                            "UPDATE dbo.SAMPLE SET Labs = ? WHERE GROUP_NAME = ?",
                            (lab, groupName),
                        )
                    conn.commit()

            df = df[df["Labs"].isin(labs)]
            if df.empty:
                print(f"Warning: No data found for labs: {labs}")
                return pd.DataFrame(columns=["ds", "Labs", "y"])

            countDf = df.groupby(["ds", "Labs"]).size().reset_index(name="y")
            print(f"Successfully loaded data for labs: {countDf['Labs'].unique().tolist()}")
            return countDf

        except Exception as e:
            print(f"Error in getLabData (attempt {attempt + 1}): {e}")
            if ("deadlock" in str(e).lower() or "40001" in str(e)) and attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            print(f"Returning empty DataFrame due to error: {e}")
            return pd.DataFrame(columns=["ds", "Labs", "y"])

    return pd.DataFrame(columns=["ds", "Labs", "y"])


def trainModel(df, modelPath):
    try:
        if df.empty:
            print("Warning: Empty DataFrame passed to trainModel")
            return None
        from neuralprophet import NeuralProphet

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
        with open(modelPath, "wb") as f:
            pickle.dump(m, f)
        return m
    except Exception as e:
        print(f"Error training model: {e}")
        return None


def _load_model(modelPath, modelDf):
    """Load a cached model; retrain on missing/incompatible pickle."""
    from ..ml import load_model

    if os.path.exists(modelPath):
        try:
            return load_model(modelPath)
        except Exception as e:
            print(f"Error loading model {modelPath}: {e}; retraining")
    return trainModel(modelDf.copy(), modelPath)


class SampleForecast(APIView):
    def get(self, request):
        try:
            months_param = request.query_params.get("months")
            if months_param is None:
                months = 6
            else:
                try:
                    months = int(months_param)
                    if months < 1 or months > 24:
                        return Response({"error": "Months must be between 1 and 24."}, status=status.HTTP_400_BAD_REQUEST)
                except ValueError:
                    return Response({"error": "Invalid months parameter. Must be a number between 1 and 24."}, status=status.HTTP_400_BAD_REQUEST)

            hist = getLabData()
            if hist.empty:
                return Response({
                    "labels": [], "values": [], "PastData": [],
                    "storyboard": "No historical data available for any lab in the specified year range.",
                    "UpperQuartile-Forecasted": 0, "LowerQuartile-Forecasted": 0,
                    "UpperQuartile-pastData": 0, "LowerQuartile-pastData": 0,
                    "warning": "No historical data available for the specified year range.",
                }, status=status.HTTP_200_OK)

            if hist["ds"].nunique() < 30:
                return Response({"error": f"Need ≥30 days of data. Found only {hist['ds'].nunique()} unique dates."}, status=status.HTTP_400_BAD_REQUEST)

            monthly = hist.copy()
            monthly["ds"] = monthly["ds"].dt.to_period("M").dt.to_timestamp()
            modelDf = monthly[
                (monthly["ds"] >= "2021-01-01") & (monthly["ds"] <= "2023-12-31")
            ].groupby("ds")["y"].sum().reset_index()

            if modelDf.empty:
                return Response({"error": "No training data available in the 2021-2023 range."}, status=status.HTTP_400_BAD_REQUEST)

            floor = modelDf["y"].min()
            m = _load_model(modelPathCombined, modelDf)
            if m is None:
                return Response({"error": "Failed to train or load model."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            start_date = datetime.datetime.now()
            end_date = start_date + pd.DateOffset(months=months) - pd.DateOffset(days=1)
            last_train = modelDf["ds"].max()
            total_months = (end_date.year - last_train.year) * 12 + (end_date.month - last_train.month) + 1

            try:
                future = m.make_future_dataframe(modelDf, periods=total_months, n_historic_predictions=False)
                forecast = m.predict(future)
                forecast["ds"] = pd.to_datetime(forecast["ds"])
            except Exception as e:
                return Response({"error": f"Forecasting failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            start_month = datetime.datetime(start_date.year, start_date.month, 1)
            end_month = datetime.datetime(end_date.year, end_date.month, 1)
            window = forecast[(forecast["ds"] >= start_month) & (forecast["ds"] <= end_month)].copy()
            if window.empty:
                return Response({"error": "No forecast points in the requested window."}, status=status.HTTP_400_BAD_REQUEST)

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
                return Response({"error": "Invalid past data range: pastEndYear must be ≥ pastStartYear."}, status=status.HTTP_400_BAD_REQUEST)

            try:
                histYearsAvailable = set(hist["ds"].dt.year.unique().tolist())
                requestedYears = set(range(pastStartYear, pastEndYear + 1))
                missingYears = sorted(list(requestedYears - histYearsAvailable))
                if missingYears:
                    missingStr = ", ".join(str(y) for y in missingYears)
                    return Response({"error": f"Past data missing for year(s): {missingStr}. Please choose a different range."}, status=status.HTTP_400_BAD_REQUEST)

                past = hist[(hist["ds"].dt.year >= pastStartYear) & (hist["ds"].dt.year <= pastEndYear)].copy()
                if past.empty:
                    return Response({"error": f"No data available in the specified year range ({pastStartYear}-{pastEndYear})."}, status=status.HTTP_400_BAD_REQUEST)

                past["monthTs"] = past["ds"].dt.to_period("M").dt.to_timestamp()
                pastMonthly = past.groupby("monthTs")["y"].sum().reset_index()
                pastMonthly["date"] = pastMonthly["monthTs"].dt.strftime("%b %Y")
                pastMonthly["samples"] = pastMonthly["y"].astype(int)
                pastData = pastMonthly[["date", "samples"]].to_dict(orient="records")
            except Exception as e:
                return Response({"error": f"Error processing past data: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            uqForecast = int(np.percentile(values, 75))
            lqForecast = int(np.percentile(values, 25))
            pastY = pastMonthly["samples"].values
            uqPast = int(np.percentile(pastY, 75)) if len(pastY) > 0 else 0
            lqPast = int(np.percentile(pastY, 25)) if len(pastY) > 0 else 0

            return Response({
                "labels": labels,
                "values": values,
                "label": "Predicted Monthly Samples (All Labs)",
                "orangeDashedLineAverage(Y-Axis)": avg,
                "orangeLine(X-Axis)": {"startDate": labels[0], "endDate": labels[-1]},
                "highestDay(Stats)": {"date": labels[maxI], "samples": values[maxI]},
                "lowestDay(Stats)": {"date": labels[minI], "samples": values[minI]},
                "storyboard": (
                    f"Based on historical data from {pastStartYear} to {pastEndYear}, overall lab activity was analyzed and forecasted. "
                    f"The model predicts monthly sample volumes from {labels[0]} to {labels[-1]}. "
                    f"The busiest forecasted month is {labels[maxI]} with {values[maxI]} samples, "
                    f"and the slowest is {labels[minI]} with {values[minI]} samples."
                ),
                "PastData": pastData,
                "UpperQuartile-Forecasted": uqForecast,
                "LowerQuartile-Forecasted": lqForecast,
                "UpperQuartile-pastData": uqPast,
                "LowerQuartile-pastData": lqPast,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"Unexpected error in SampleForecast: {e}")
            return Response({"error": f"Unexpected error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SectionForecast(APIView):
    def get(self, request):
        lab = request.query_params.get("lab")
        try:
            if lab not in labs:
                return Response({"error": f"Provide valid lab ∈ {labs}."}, status=status.HTTP_400_BAD_REQUEST)

            months_param = request.query_params.get("months")
            if months_param is None:
                months = 6
            else:
                try:
                    months = int(months_param)
                    if months < 1 or months > 24:
                        return Response({"error": "Months must be between 1 and 24."}, status=status.HTTP_400_BAD_REQUEST)
                except ValueError:
                    return Response({"error": "Invalid months parameter. Must be a number between 1 and 24."}, status=status.HTTP_400_BAD_REQUEST)

            hist = getLabData()
            if hist.empty:
                return Response({
                    "lab": lab, "labels": [], "values": [], "PastData": [],
                    "storyboard": f"No data available for lab '{lab}' in the specified year range.",
                    "forecastUpperQuartile": 0, "forecastLowerQuartile": 0,
                    "pastUpperQuartile": 0, "pastLowerQuartile": 0,
                    "warning": "No historical data available for this lab and year range.",
                }, status=status.HTTP_200_OK)

            section = hist[hist["Labs"] == lab].copy()
            if section.empty:
                return Response({
                    "lab": lab, "labels": [], "values": [], "PastData": [],
                    "storyboard": f"No data found for lab '{lab}' in the specified year range.",
                    "forecastUpperQuartile": 0, "forecastLowerQuartile": 0,
                    "pastUpperQuartile": 0, "pastLowerQuartile": 0,
                    "warning": f"Lab '{lab}' has no data in the specified year range.",
                }, status=status.HTTP_200_OK)

            if section["ds"].nunique() < 30:
                return Response({"error": f"Need ≥30 days of data for '{lab}'. Found only {section['ds'].nunique()} unique dates."}, status=status.HTTP_400_BAD_REQUEST)

            section["ds"] = section["ds"].dt.to_period("M").dt.to_timestamp()
            modelDf = section[
                (section["ds"] >= "2021-01-01") & (section["ds"] <= "2023-12-31")
            ].groupby("ds")["y"].sum().reset_index()
            if modelDf.empty:
                return Response({"error": f"No training data available for lab '{lab}' in the 2021-2023 range."}, status=status.HTTP_400_BAD_REQUEST)

            floor = modelDf["y"].min()
            # Section names can contain "/" and spaces (e.g. "Gasoline/Mogas") —
            # sanitise to a safe model filename.
            safe_lab = lab.replace("/", "-").replace(" ", "_")
            modelPath = modelPathLabType.format(lab=safe_lab)
            m = _load_model(modelPath, modelDf)
            if m is None:
                return Response({"error": f"Failed to train or load model for lab '{lab}'."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            start_date = datetime.datetime.now()
            end_date = start_date + pd.DateOffset(months=months) - pd.DateOffset(days=1)
            last_train = modelDf["ds"].max()
            total_months = (end_date.year - last_train.year) * 12 + (end_date.month - last_train.month) + 1

            try:
                future = m.make_future_dataframe(modelDf, periods=total_months, n_historic_predictions=False)
                forecast = m.predict(future)
                forecast["ds"] = pd.to_datetime(forecast["ds"])
            except Exception as e:
                return Response({"error": f"Forecasting failed for lab '{lab}': {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            start_month = datetime.datetime(start_date.year, start_date.month, 1)
            end_month = datetime.datetime(end_date.year, end_date.month, 1)
            window = forecast[(forecast["ds"] >= start_month) & (forecast["ds"] <= end_month)].copy()
            if window.empty:
                return Response({"error": "No forecast points in the requested window."}, status=status.HTTP_400_BAD_REQUEST)

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
                return Response({"error": "Invalid past data range: pastEndYear must be ≥ pastStartYear."}, status=status.HTTP_400_BAD_REQUEST)

            try:
                sectionYearsAvailable = set(section["ds"].dt.year.unique().tolist())
                requestedYears = set(range(pastStartYear, pastEndYear + 1))
                missingYears = sorted(list(requestedYears - sectionYearsAvailable))
                if missingYears:
                    missingStr = ", ".join(str(y) for y in missingYears)
                    return Response({"error": f"Past data missing for year(s): {missingStr} in lab '{lab}'. Please choose a different range."}, status=status.HTTP_400_BAD_REQUEST)

                past = section[(section["ds"].dt.year >= pastStartYear) & (section["ds"].dt.year <= pastEndYear)].copy()
                if past.empty:
                    return Response({"error": f"No data available for lab '{lab}' in the specified year range ({pastStartYear}-{pastEndYear})."}, status=status.HTTP_400_BAD_REQUEST)

                past["monthTs"] = past["ds"].dt.to_period("M").dt.to_timestamp()
                pastMonthly = past.groupby("monthTs")["y"].sum().reset_index()
                pastMonthly["date"] = pastMonthly["monthTs"].dt.strftime("%b %Y")
                pastMonthly["samples"] = pastMonthly["y"].astype(int)
                pastData = pastMonthly[["date", "samples"]].to_dict(orient="records")
            except Exception as e:
                return Response({"error": f"Error processing past data for lab '{lab}': {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            uqForecast = int(np.percentile(values, 75))
            lqForecast = int(np.percentile(values, 25))
            pastY = pastMonthly["samples"].values
            uqPast = int(np.percentile(pastY, 75)) if len(pastY) > 0 else 0
            lqPast = int(np.percentile(pastY, 25)) if len(pastY) > 0 else 0

            return Response({
                "lab": lab,
                "labels": labels,
                "values": values,
                "label": f"Predicted Monthly Samples ({lab})",
                "orangeDashedLineAverage(Y-Axis)": avg,
                "orangeLine(X-Axis)": {"startDate": labels[0], "endDate": labels[-1]},
                "highestDay(Stats)": {"date": labels[maxI], "samples": values[maxI]},
                "lowestDay(Stats)": {"date": labels[minI], "samples": values[minI]},
                "storyboard": (
                    f"For the '{lab}' section, historical data from {pastStartYear} to {pastEndYear} was used to forecast upcoming trends. "
                    f"The prediction covers {labels[0]} to {labels[-1]}, with {labels[maxI]} being the busiest at {values[maxI]} samples, "
                    f"and {labels[minI]} being the lowest at {values[minI]} samples."
                ),
                "PastData": pastData,
                "forecastUpperQuartile": uqForecast,
                "forecastLowerQuartile": lqForecast,
                "pastUpperQuartile": uqPast,
                "pastLowerQuartile": lqPast,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"Unexpected error in SectionForecast for lab {lab}: {e}")
            return Response({"error": f"Unexpected error processing lab '{lab}': {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
