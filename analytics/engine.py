"""Statistical analysis engine — pandas-backed math, aggregation and
comparative analysis over datasets, plus safe calculated-field expressions.
"""
import ast
import math
import operator

import pandas as pd

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

AGGREGATIONS = {"sum", "count", "avg", "min", "max", "std", "var", "median"}


class AnalyticsError(Exception):
    """Raised for invalid analytics requests or expressions."""


# --------------------------------------------------------------------------
# Calculated fields — safe arithmetic expressions over columns
# --------------------------------------------------------------------------
def _eval_node(node, df):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, df)
    if isinstance(node, ast.BinOp):
        func = _BINARY_OPS.get(type(node.op))
        if func is None:
            raise AnalyticsError("Operator not allowed in expression.")
        return func(_eval_node(node.left, df), _eval_node(node.right, df))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_node(node.operand, df)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in df.columns:
            raise AnalyticsError(f"Unknown column '{node.id}' in expression.")
        return pd.to_numeric(df[node.id], errors="coerce")
    raise AnalyticsError("Expression contains an unsupported element.")


def evaluate_expression(df, expression):
    """Safely evaluate an arithmetic expression (columns, numbers, + - * / % **)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise AnalyticsError(f"Invalid expression: {exc.msg}") from exc
    return _eval_node(tree, df)


def build_dataframe(dataset):
    """Build a pandas DataFrame for a dataset, applying its calculated fields.
    Refreshes the dataset first if it has never been run."""
    if not dataset.last_refreshed_at:
        from datasets.services import refresh_dataset

        refresh_dataset(dataset)

    df = pd.DataFrame(
        dataset.cached_rows or [], columns=dataset.cached_columns or None
    )
    for field in dataset.calculated_fields or []:
        name = (field or {}).get("name")
        expression = (field or {}).get("expression")
        if not name or not expression:
            continue
        try:
            df[name] = evaluate_expression(df, expression)
        except AnalyticsError:
            df[name] = None
    return df


# --------------------------------------------------------------------------
# JSON-safe coercion
# --------------------------------------------------------------------------
def _num(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return round(result, 4)


def _cell(value):
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 4)
    return value


def df_to_rows(df):
    return [[_cell(v) for v in row] for row in df.itertuples(index=False, name=None)]


# --------------------------------------------------------------------------
# Descriptive statistics
# --------------------------------------------------------------------------
def column_statistics(df):
    """Per-column descriptive statistics for a dataset."""
    stats = []
    for col in df.columns:
        series = df[col]
        numeric = pd.to_numeric(series, errors="coerce")
        non_null = int(series.notna().sum())
        is_numeric = non_null > 0 and int(numeric.notna().sum()) >= non_null * 0.8
        entry = {
            "column": str(col),
            "count": non_null,
            "null_count": int(series.isna().sum()),
            "distinct": int(series.nunique(dropna=True)),
        }
        if is_numeric:
            clean = numeric.dropna()
            entry.update({
                "type": "numeric",
                "sum": _num(clean.sum()),
                "mean": _num(clean.mean()),
                "min": _num(clean.min()),
                "max": _num(clean.max()),
                "std": _num(clean.std()),
                "variance": _num(clean.var()),
                "median": _num(clean.median()),
                "p25": _num(clean.quantile(0.25)) if len(clean) else None,
                "p75": _num(clean.quantile(0.75)) if len(clean) else None,
            })
        else:
            top = series.astype(str).value_counts().head(5)
            entry.update({
                "type": "text",
                "top_values": [
                    {"value": str(k), "count": int(v)} for k, v in top.items()
                ],
            })
        stats.append(entry)
    return {
        "row_count": int(len(df)),
        "column_count": len(df.columns),
        "statistics": stats,
    }


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def aggregate(df, group_by, measure, aggregation):
    """Group a dataset by a column and aggregate a measure."""
    aggregation = (aggregation or "count").lower()
    if aggregation not in AGGREGATIONS:
        raise AnalyticsError(f"Unsupported aggregation '{aggregation}'.")
    if not group_by or group_by not in df.columns:
        raise AnalyticsError("A valid 'group_by' column is required.")

    columns = [group_by, "value", "pct_of_total"]
    if df.empty:
        return {"group_by": group_by, "measure": measure,
                "aggregation": aggregation, "columns": columns, "rows": []}

    groups = df[group_by].fillna("(blank)").astype(str)
    if aggregation == "count":
        series = groups.groupby(groups).size()
    else:
        if not measure or measure not in df.columns:
            raise AnalyticsError("A valid 'measure' column is required.")
        values = pd.to_numeric(df[measure], errors="coerce")
        grouped = values.groupby(groups)
        series = {
            "sum": grouped.sum, "avg": grouped.mean, "min": grouped.min,
            "max": grouped.max, "std": grouped.std, "var": grouped.var,
            "median": grouped.median,
        }[aggregation]()

    series = series.sort_values(ascending=False)
    total = float(series.sum() or 0)
    rows = []
    for label, value in series.items():
        clean = _num(value)
        pct = _num((clean / total * 100) if (total and clean is not None) else 0)
        rows.append([str(label), clean, pct])
    return {
        "group_by": group_by, "measure": measure, "aggregation": aggregation,
        "columns": columns, "rows": rows,
    }


# --------------------------------------------------------------------------
# Comparative analysis
# --------------------------------------------------------------------------
def compare(df, dimension, measure, aggregation):
    """Compare a measure across a dimension — adds rank and vs-average deltas."""
    base = aggregate(df, dimension, measure, aggregation)
    rows = base["rows"]
    values = [r[1] for r in rows if r[1] is not None]
    average = (sum(values) / len(values)) if values else 0.0

    out = []
    for index, row in enumerate(rows):
        value = row[1] or 0
        out.append([
            row[0], row[1], row[2], index + 1,
            _num(value - average),
            _num((value / average - 1) * 100) if average else None,
        ])
    return {
        "dimension": dimension, "measure": measure, "aggregation": aggregation,
        "average": _num(average),
        "columns": [
            dimension, "value", "pct_of_total", "rank",
            "vs_average", "vs_average_pct",
        ],
        "rows": out,
    }
