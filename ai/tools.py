"""Agent tool registry — the capabilities the AI agent can call.

Every tool here is a *thin, permission-scoped wrapper* over functionality the
platform already ships (datasets, the analytics engine, the predictor, the
deterministic NL query engine and document retrieval). The agent's LLM only
*orchestrates and narrates*; the deterministic tools below do the actual math
and data access, so figures are never hallucinated.

Design rules:
  * Read-only. No tool mutates data or runs arbitrary code.
  * Permission-scoped. Dataset tools enforce ``dataset.accessible_by(user)``.
  * Self-describing. The ``description`` + ``input_schema`` are what the model
    sees — keep them precise; tool-use quality depends on them.
  * Fail soft. ``execute_tool`` converts any error into ``{"error": ...}`` so the
    agent can recover and try a different approach instead of crashing the loop.

A tool function has the signature ``fn(user, **arguments) -> dict`` and must
return a JSON-serialisable dict (see :func:`ai.agent` for serialisation).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django.db.models import Q

from analytics import engine, predict, semantic
from datasets.models import Dataset

# Result-size caps — keep tool output small so it fits the model context cheaply.
MAX_ROWS = 50
MAX_SAMPLE_ROWS = 8
MAX_ANOMALIES = 40
MAX_DOC_CHARS = 700
MAX_DOC_HITS = 6


class ToolError(Exception):
    """A tool could not run as asked (bad args, no access, no data)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _accessible_datasets(user):
    if getattr(user, "is_admin", False):
        return Dataset.objects.all()
    return Dataset.objects.filter(Q(owner=user) | Q(shared_with=user)).distinct()


def _get_dataset(user, dataset_id):
    if dataset_id is None:
        raise ToolError("A 'dataset_id' is required. Call list_datasets first.")
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None:
        raise ToolError(f"No dataset with id {dataset_id}.")
    if not dataset.accessible_by(user):
        raise ToolError(f"You do not have access to dataset {dataset_id}.")
    return dataset


def _get_df(user, dataset_id):
    dataset = _get_dataset(user, dataset_id)
    df = engine.build_dataframe(dataset)
    return dataset, df


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------
def _list_datasets(user, search: str = "", **_):
    qs = _accessible_datasets(user)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
    # Keep this LIGHT — discovery only. Columns come from describe_dataset on
    # demand, so a big workspace can't blow the model's context window here.
    items = []
    for ds in qs.order_by("name")[:60]:
        cols = [str(c) for c in (ds.cached_columns or [])]
        items.append({
            "id": ds.id,
            "name": ds.name,
            "row_count": ds.row_count,
            "n_columns": len(cols),
            "columns_preview": cols[:6],
        })
    return {"count": len(items), "datasets": items,
            "hint": "Use describe_dataset(dataset_id) for full columns + stats."}


def _describe_dataset(user, dataset_id=None, **_):
    dataset, df = _get_df(user, dataset_id)
    stats = engine.column_statistics(df)
    sample = {
        "columns": [str(c) for c in df.columns],
        "rows": engine.df_to_rows(df.head(MAX_SAMPLE_ROWS)),
    }
    return {
        "dataset_id": dataset.id,
        "name": dataset.name,
        "row_count": stats["row_count"],
        "column_count": stats["column_count"],
        "statistics": stats["statistics"],
        "sample": sample,
    }


def _aggregate_data(user, dataset_id=None, group_by=None, measure=None,
                    aggregation="count", **_):
    _, df = _get_df(user, dataset_id)
    result = engine.aggregate(df, group_by, measure, aggregation)
    result["rows"] = result["rows"][:MAX_ROWS]
    result["truncated"] = len(result["rows"]) >= MAX_ROWS
    return result


def _compare_dimension(user, dataset_id=None, dimension=None, measure=None,
                       aggregation="sum", **_):
    _, df = _get_df(user, dataset_id)
    result = engine.compare(df, dimension, measure, aggregation)
    result["rows"] = result["rows"][:MAX_ROWS]
    return result


def _correlate_columns(user, dataset_id=None, method="pearson", **_):
    _, df = _get_df(user, dataset_id)
    result = engine.correlation_matrix(df, method=method)
    # Surface the strongest off-diagonal pairs — easier for the model to reason on.
    cols, matrix = result["columns"], result["matrix"]
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = matrix[i][j]
            if v is not None:
                pairs.append({"a": cols[i], "b": cols[j], "r": v})
    pairs.sort(key=lambda p: abs(p["r"]), reverse=True)
    result["strongest_pairs"] = pairs[:10]
    return result


def _detect_anomalies(user, dataset_id=None, column=None, method="zscore",
                      threshold=3.0, **_):
    _, df = _get_df(user, dataset_id)
    result = predict.detect_anomalies(df, column, method=method, threshold=threshold)
    # Drop the full per-point arrays from the tool view — only the flagged ones matter.
    result.pop("values", None)
    result.pop("flags", None)
    result["anomalies"] = result["anomalies"][:MAX_ANOMALIES]
    return result


def _forecast_series(user, dataset_id=None, column=None, periods=6, **_):
    _, df = _get_df(user, dataset_id)
    return predict.forecast(df, column, periods=int(periods or 6))


def _run_statistical_test(user, dataset_id=None, test=None, params=None, **_):
    from analytics import stats_catalog
    from analytics.engine import AnalyticsError

    if not test:
        raise ToolError("A 'test' name is required.")
    _, df = _get_df(user, dataset_id)
    try:
        return stats_catalog.run_test(test, df, params or {})
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _detect_anomalies_ml(user, dataset_id=None, columns=None,
                         method="isolation_forest", contamination=0.05, **_):
    from analytics import anomaly
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return anomaly.detect_multivariate(df, columns, method=method,
                                           contamination=float(contamination or 0.05))
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _root_cause(user, dataset_id=None, target_column=None, target_value=None,
                op=None, threshold=None, feature_columns=None, **_):
    from analytics import rootcause
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return rootcause.driver_analysis(
            df, target_column, target_value=target_value, op=op,
            threshold=threshold, feature_columns=feature_columns)
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _cluster_data(user, dataset_id=None, columns=None, method="kmeans", k=4, **_):
    from analytics import rootcause
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return rootcause.cluster_analysis(df, columns, method=method, k=int(k or 4))
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _find_associations(user, dataset_id=None, columns=None, target_column=None,
                       target_value=None, min_support=0.05, min_confidence=0.5, **_):
    from analytics import rootcause
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return rootcause.association_rules_mining(
            df, columns, target_column=target_column, target_value=target_value,
            min_support=float(min_support or 0.05),
            min_confidence=float(min_confidence or 0.5))
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _forecast_metric(user, dataset_id=None, date_column=None, value_column=None,
                     agg="count", freq="M", periods=6, group_by=None, **_):
    from analytics import forecasting_ext
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return forecasting_ext.forecast_metric(
            df, date_column, value_column=value_column, agg=agg, freq=freq,
            periods=int(periods or 6), group_by=group_by)
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _discover_relationships(user, dataset_id=None, **_):
    from analytics import relationships
    from analytics.engine import AnalyticsError
    _, df = _get_df(user, dataset_id)
    try:
        return relationships.discover_relationships(df)
    except AnalyticsError as exc:
        raise ToolError(str(exc))


def _suggest_dataset_links(user, **_):
    from analytics import relationships
    qs = _accessible_datasets(user)
    meta = [{"id": d.id, "name": d.name,
             "columns": [str(c) for c in (d.cached_columns or [])]}
            for d in qs[:100]]
    return relationships.suggest_links(meta)


def _query_dataset(user, dataset_id=None, question=None, **_):
    """Deterministic natural-language question over a dataset (no LLM, fully
    explainable). Best for "how many / total / average / top N by ..." style
    asks; it returns the exact interpretation it executed."""
    dataset, df = _get_df(user, dataset_id)
    if not question:
        raise ToolError("A 'question' is required.")
    result = semantic.answer_question(df, question, dataset_name=dataset.name)
    if isinstance(result, dict) and isinstance(result.get("rows"), list):
        result["rows"] = result["rows"][:MAX_ROWS]
    return result


def _search_documents(user, query=None, top_k=MAX_DOC_HITS, **_):
    """Retrieve the most relevant passages from the indexed document corpus
    (SOPs, ASTM specs, training docs). Returns excerpts with their source so the
    agent can quote numerical specifications and requirements."""
    if not query:
        raise ToolError("A 'query' is required.")
    from docsearch.index_store import get_index
    from docsearch.visibility import visible_owner_keys

    index = get_index()
    rows, meta = index.retrieve(
        query, top_k=int(top_k or MAX_DOC_HITS), allowed_owners=visible_owner_keys(user)
    )
    hits = []
    for row in rows[:MAX_DOC_HITS]:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k] if k in row else d)
        text = (get("chunk_text", "") or "")[:MAX_DOC_CHARS]
        hits.append({
            "source": get("doc_id", "") or get("doc_title", ""),
            "page": get("page", None),
            "excerpt": text,
        })
    return {
        "query": query,
        "hit_count": len(hits),
        "best_score": round(float(meta.get("best_score", 0.0)), 4),
        "passages": hits,
        "note": "No matching documents found." if not hits else "",
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def _ds_id_prop(desc="Numeric id of the dataset (from list_datasets)."):
    return {"type": "integer", "description": desc}


TOOLS: dict[str, Tool] = {
    t.name: t for t in [
        Tool(
            name="list_datasets",
            description=(
                "List the datasets the current user can access (id, name, "
                "description, row count, column names). ALWAYS call this first to "
                "discover what data exists and to get the dataset_id + exact column "
                "names other tools need."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Optional case-insensitive filter on name/description.",
                    },
                },
            },
            fn=_list_datasets,
        ),
        Tool(
            name="describe_dataset",
            description=(
                "Get the schema and descriptive statistics for one dataset: per-"
                "column type, count, nulls, min/max/mean/median/std for numeric "
                "columns, top values for text columns, plus a few sample rows. Use "
                "this to learn exact column names and value ranges before analysing."
            ),
            input_schema={
                "type": "object",
                "properties": {"dataset_id": _ds_id_prop()},
                "required": ["dataset_id"],
            },
            fn=_describe_dataset,
        ),
        Tool(
            name="aggregate_data",
            description=(
                "Group a dataset by a column and aggregate a measure. Returns rows "
                "of (group, value, pct_of_total), sorted by value. Use aggregation "
                "'count' to count rows per group (no measure needed); otherwise give "
                "a numeric 'measure' column and one of sum/avg/min/max/std/var/median."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "group_by": {"type": "string", "description": "Column to group by (exact name)."},
                    "measure": {"type": "string", "description": "Numeric column to aggregate (omit for count)."},
                    "aggregation": {
                        "type": "string",
                        "enum": ["count", "sum", "avg", "min", "max", "std", "var", "median"],
                        "description": "How to aggregate the measure.",
                    },
                },
                "required": ["dataset_id", "group_by", "aggregation"],
            },
            fn=_aggregate_data,
        ),
        Tool(
            name="compare_dimension",
            description=(
                "Compare a measure across the values of a dimension, adding rank and "
                "deltas vs the average (vs_average, vs_average_pct). Use this to find "
                "over/under-performers — e.g. compare average turnaround time across "
                "lab sections, or out-of-spec rate across instruments/analysts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "dimension": {"type": "string", "description": "Column to break down by (exact name)."},
                    "measure": {"type": "string", "description": "Numeric column to aggregate (omit for count)."},
                    "aggregation": {
                        "type": "string",
                        "enum": ["count", "sum", "avg", "min", "max", "std", "var", "median"],
                    },
                },
                "required": ["dataset_id", "dimension", "aggregation"],
            },
            fn=_compare_dimension,
        ),
        Tool(
            name="correlate_columns",
            description=(
                "Compute pairwise correlations across all numeric columns of a "
                "dataset and return the strongest pairs. Use to surface hidden "
                "relationships between variables. method='pearson' (linear) or "
                "'spearman' (rank/monotonic)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "method": {"type": "string", "enum": ["pearson", "spearman"]},
                },
                "required": ["dataset_id"],
            },
            fn=_correlate_columns,
        ),
        Tool(
            name="detect_anomalies",
            description=(
                "Flag outliers in one numeric column using the z-score rule "
                "(default, threshold in std-devs) or the IQR rule (method='iqr'). "
                "Returns the flagged points with their values/scores and the bounds. "
                "Use to find unusual results worth investigating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "column": {"type": "string", "description": "Numeric column to scan (exact name)."},
                    "method": {"type": "string", "enum": ["zscore", "iqr"]},
                    "threshold": {"type": "number", "description": "Z-score threshold (default 3.0); ignored for IQR."},
                },
                "required": ["dataset_id", "column"],
            },
            fn=_detect_anomalies,
        ),
        Tool(
            name="run_statistical_test",
            description=(
                "Run a FORMAL statistical test and get the statistic, p-value and a "
                "plain-English interpretation. Available tests: descriptive, normality, "
                "trend, t_test_one_sample, t_test_two_sample, anova_oneway, "
                "f_test_variances, chi_square, correlation, regression, outliers, "
                "control_chart, process_capability. Put test-specific args in 'params' "
                "— e.g. anova_oneway:{value_column, group_column}; correlation:{column1, "
                "column2, method}; regression:{y_column, x_column|x_columns, kind}; "
                "process_capability:{column, lsl, usl}; t_test_one_sample:{column, "
                "popmean}. Call describe_dataset first to get exact column names."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "test": {
                        "type": "string",
                        "enum": [
                            "descriptive", "normality", "trend",
                            "t_test_one_sample", "t_test_two_sample", "anova_oneway",
                            "f_test_variances", "chi_square", "correlation",
                            "regression", "outliers", "control_chart",
                            "process_capability",
                        ],
                        "description": "Which statistical test to run.",
                    },
                    "params": {
                        "type": "object",
                        "description": "Test-specific parameters (column names, alpha, "
                                       "spec limits, method, etc.).",
                    },
                },
                "required": ["dataset_id", "test", "params"],
            },
            fn=_run_statistical_test,
        ),
        Tool(
            name="forecast_series",
            description=(
                "Forecast the next N points of a numeric column (linear trend + "
                "Holt smoothing), with a confidence band and a trend summary "
                "(slope, r_squared, direction). For a quick projection of a metric "
                "series already in a dataset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "column": {"type": "string", "description": "Numeric column to forecast (exact name)."},
                    "periods": {"type": "integer", "description": "How many future points (default 6)."},
                },
                "required": ["dataset_id", "column"],
            },
            fn=_forecast_series,
        ),
        Tool(
            name="query_dataset",
            description=(
                "Ask a plain-English question about ONE dataset and get a "
                "deterministic, fully-explainable answer (no guessing). Good for "
                "'how many…', 'total/average … by …', 'top N … by …'. Returns the "
                "exact interpretation it executed plus the result rows. Prefer this "
                "for straightforward lookups; use aggregate_data/compare_dimension "
                "when you need precise control over grouping."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "question": {"type": "string", "description": "The natural-language question."},
                },
                "required": ["dataset_id", "question"],
            },
            fn=_query_dataset,
        ),
        Tool(
            name="detect_anomalies_ml",
            description=(
                "Detect MULTIVARIATE anomalies — rows that are unusual across several "
                "numeric columns at once (not just one) — using IsolationForest "
                "(default) or LocalOutlierFactor (method='local_outlier_factor'). "
                "Returns the flagged rows with their values. Use when a problem may "
                "hide in the COMBINATION of measurements. For a single column, use "
                "detect_anomalies instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Numeric columns (omit = all numeric)."},
                    "method": {"type": "string",
                               "enum": ["isolation_forest", "local_outlier_factor"]},
                    "contamination": {"type": "number",
                                      "description": "Expected outlier fraction (default 0.05)."},
                },
                "required": ["dataset_id"],
            },
            fn=_detect_anomalies_ml,
        ),
        Tool(
            name="root_cause",
            description=(
                "ROOT-CAUSE analysis: given an OUTCOME, rank the factors most "
                "associated with it. Define the outcome on target_column either by "
                "target_value (categorical, e.g. STATUS_LABEL='Rejected') or by "
                "op+threshold (numeric, e.g. TAT_HOURS gt 24 = 'late'). Returns a "
                "lift table (e.g. 'rejection rate is 3× higher for instrument=GC-401') "
                "plus a RandomForest ranking of which columns matter most. Use to find "
                "WHY abnormal results happen — by analyst, instrument, section, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "target_column": {"type": "string", "description": "The outcome column."},
                    "target_value": {"type": "string",
                                     "description": "Outcome = this value (categorical)."},
                    "op": {"type": "string", "enum": ["gt", "gte", "lt", "lte", "eq"],
                           "description": "Numeric outcome comparison."},
                    "threshold": {"type": "number", "description": "Numeric outcome threshold."},
                    "feature_columns": {"type": "array", "items": {"type": "string"},
                                        "description": "Candidate factors (omit = auto)."},
                },
                "required": ["dataset_id", "target_column"],
            },
            fn=_root_cause,
        ),
        Tool(
            name="cluster_data",
            description=(
                "Cluster rows into natural groups by their numeric columns (KMeans "
                "default, or DBSCAN) and return each cluster's size and feature-mean "
                "profile. Use to segment samples/results into behaviour groups."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Numeric columns (omit = all numeric)."},
                    "method": {"type": "string", "enum": ["kmeans", "dbscan"]},
                    "k": {"type": "integer", "description": "Number of clusters (KMeans, default 4)."},
                },
                "required": ["dataset_id"],
            },
            fn=_cluster_data,
        ),
        Tool(
            name="find_associations",
            description=(
                "Association-rule mining (apriori) over categorical columns — finds "
                "co-occurrence patterns like '{instrument=GC-401, priority=Routine} ⟹ "
                "{result=Out-of-spec}' with support/confidence/lift. Optionally target "
                "a specific outcome via target_column + target_value. Use to surface "
                "hidden relationships across samples, tests, instruments and personnel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "columns": {"type": "array", "items": {"type": "string"},
                                "description": "Categorical columns to mine."},
                    "target_column": {"type": "string", "description": "Optional outcome column."},
                    "target_value": {"type": "string", "description": "Optional outcome value."},
                    "min_support": {"type": "number", "description": "Min support (default 0.05)."},
                    "min_confidence": {"type": "number", "description": "Min confidence (default 0.5)."},
                },
                "required": ["dataset_id", "columns"],
            },
            fn=_find_associations,
        ),
        Tool(
            name="forecast_metric",
            description=(
                "Forecast an OPERATIONAL metric over time by resampling a dataset on "
                "a date column. Covers workforce/personnel (agg=count, group_by=analyst), "
                "productivity/time (value=TAT column, agg=mean), and cost/resource "
                "(value=cost, agg=sum). freq is D/W/M/Q/Y. Optionally one series per "
                "group_by value. Returns history + projection + trend. Use for "
                "'forecast next quarter's workload / turnaround / cost'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset_id": _ds_id_prop(),
                    "date_column": {"type": "string", "description": "Date/time column to resample on."},
                    "value_column": {"type": "string", "description": "Numeric column to aggregate (omit for count)."},
                    "agg": {"type": "string",
                            "enum": ["count", "sum", "mean", "min", "max", "median"]},
                    "freq": {"type": "string", "enum": ["D", "W", "M", "Q", "Y"],
                             "description": "Resample frequency (default M)."},
                    "periods": {"type": "integer", "description": "Periods to forecast (default 6)."},
                    "group_by": {"type": "string", "description": "Optional: one forecast per group value."},
                },
                "required": ["dataset_id", "date_column"],
            },
            fn=_forecast_metric,
        ),
        Tool(
            name="discover_relationships",
            description=(
                "Scan a dataset for the strongest HIDDEN relationships among its "
                "columns: numeric↔numeric correlations, categorical↔categorical "
                "associations (Cramér's V), and category→numeric effects (which "
                "category explains a numeric column's variance). Use for open-ended "
                "'what's related to what / what drives what' contextual-intelligence "
                "questions."
            ),
            input_schema={
                "type": "object",
                "properties": {"dataset_id": _ds_id_prop()},
                "required": ["dataset_id"],
            },
            fn=_discover_relationships,
        ),
        Tool(
            name="suggest_dataset_links",
            description=(
                "Find columns SHARED across the user's datasets (candidate join "
                "keys) to enable cross-functional analysis — e.g. linking lab, "
                "operational, maintenance and calibration datasets. Takes no "
                "arguments; scans all accessible datasets."
            ),
            input_schema={"type": "object", "properties": {}},
            fn=_suggest_dataset_links,
        ),
        Tool(
            name="search_documents",
            description=(
                "Search the indexed document corpus (SOPs, ASTM/industry standards, "
                "training material) and return the most relevant passages with their "
                "source. Use to ground answers in written specifications — e.g. to "
                "quote a numerical spec range or a procedural requirement — and to "
                "check whether a data finding complies with a documented limit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for in the documents."},
                    "top_k": {"type": "integer", "description": "How many passages (default 6)."},
                },
                "required": ["query"],
            },
            fn=_search_documents,
        ),
    ]
}


def tool_schemas() -> list[dict]:
    """The neutral tool schemas to advertise to the model."""
    return [t.schema() for t in TOOLS.values()]


def execute_tool(name: str, user, arguments: dict) -> dict:
    """Run a tool by name. Never raises — failures come back as ``{"error": ...}``
    so the agent loop can show the model what went wrong and let it recover."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'. Available: {', '.join(TOOLS)}."}
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return tool.fn(user, **args)
    except ToolError as exc:
        return {"error": str(exc)}
    except TypeError as exc:  # bad/extra arguments from the model
        return {"error": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - tools must fail soft for the agent
        return {"error": f"{name} failed: {exc}"}
