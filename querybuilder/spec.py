"""Validation & normalization of the visual query-builder spec.

The spec is the canonical JSON representation of a query — it is what gets
saved, compiled to SQL and executed.
"""


class SpecError(Exception):
    """Raised when a query spec is structurally invalid."""


AGGREGATES = {"COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX", "STDDEV"}
OPERATORS = {
    "=", "!=", ">", "<", ">=", "<=",
    "LIKE", "NOT LIKE", "IN", "NOT IN",
    "IS NULL", "IS NOT NULL", "BETWEEN",
}
JOIN_TYPES = {"INNER", "LEFT", "RIGHT", "FULL"}
DIRECTIONS = {"ASC", "DESC"}


def _require(condition, message):
    if not condition:
        raise SpecError(message)


def _table_ref(raw, context):
    """Normalize a table reference (string or {schema, name}) -> dict."""
    if isinstance(raw, str) and raw.strip():
        return {"schema": None, "name": raw.strip()}
    if isinstance(raw, dict) and raw.get("name"):
        return {
            "schema": (raw.get("schema") or None),
            "name": str(raw["name"]).strip(),
        }
    raise SpecError(f"Invalid table reference for {context}.")


def validate_spec(spec):
    """Validate a raw query spec; return a normalized copy with every section
    present. Raises SpecError on any structural problem."""
    _require(isinstance(spec, dict), "Query spec must be an object.")

    base = _table_ref(spec.get("base_table"), "base_table")

    raw_columns = spec.get("columns") or []
    _require(
        isinstance(raw_columns, list) and len(raw_columns) > 0,
        "Select at least one column.",
    )
    columns = []
    for i, c in enumerate(raw_columns):
        _require(isinstance(c, dict), f"columns[{i}] must be an object.")
        table = (c.get("table") or "").strip()
        col = (c.get("column") or "").strip()
        _require(table and col, f"columns[{i}] needs a table and a column.")
        agg = c.get("aggregate")
        if agg:
            agg = str(agg).upper()
            _require(agg in AGGREGATES, f"columns[{i}] has unknown aggregate '{agg}'.")
        columns.append({
            "table": table,
            "column": col,
            "alias": (c.get("alias") or "").strip() or None,
            "aggregate": agg or None,
        })

    joins = []
    for i, j in enumerate(spec.get("joins") or []):
        _require(isinstance(j, dict), f"joins[{i}] must be an object.")
        join_type = str(j.get("join_type") or "INNER").upper()
        _require(join_type in JOIN_TYPES, f"joins[{i}] has unknown join type '{join_type}'.")
        left_table = (j.get("left_table") or "").strip()
        left_column = (j.get("left_column") or "").strip()
        right_column = (j.get("right_column") or "").strip()
        right_table = _table_ref(j.get("right_table"), f"joins[{i}].right_table")
        _require(
            left_table and left_column and right_column,
            f"joins[{i}] needs left_table, left_column and right_column.",
        )
        joins.append({
            "join_type": join_type,
            "left_table": left_table,
            "left_column": left_column,
            "right_table": right_table,
            "right_column": right_column,
        })

    filters = []
    for i, f in enumerate(spec.get("filters") or []):
        _require(isinstance(f, dict), f"filters[{i}] must be an object.")
        operator = str(f.get("operator") or "=").upper()
        _require(operator in OPERATORS, f"filters[{i}] has unknown operator '{operator}'.")
        table = (f.get("table") or "").strip()
        col = (f.get("column") or "").strip()
        _require(table and col, f"filters[{i}] needs a table and a column.")
        connector = str(f.get("connector") or "AND").upper()
        _require(connector in ("AND", "OR"), f"filters[{i}] connector must be AND or OR.")
        filters.append({
            "table": table, "column": col, "operator": operator,
            "value": f.get("value"), "connector": connector,
        })

    group_by = []
    for i, g in enumerate(spec.get("group_by") or []):
        _require(isinstance(g, dict), f"group_by[{i}] must be an object.")
        table = (g.get("table") or "").strip()
        col = (g.get("column") or "").strip()
        _require(table and col, f"group_by[{i}] needs a table and a column.")
        group_by.append({"table": table, "column": col})

    having = []
    for i, h in enumerate(spec.get("having") or []):
        _require(isinstance(h, dict), f"having[{i}] must be an object.")
        agg = str(h.get("aggregate") or "").upper()
        _require(agg in AGGREGATES, f"having[{i}] needs a valid aggregate.")
        operator = str(h.get("operator") or "=").upper()
        _require(operator in OPERATORS, f"having[{i}] has unknown operator '{operator}'.")
        table = (h.get("table") or "").strip()
        col = (h.get("column") or "").strip()
        _require(table and col, f"having[{i}] needs a table and a column.")
        having.append({
            "table": table, "column": col, "aggregate": agg,
            "operator": operator, "value": h.get("value"),
        })

    order_by = []
    for i, o in enumerate(spec.get("order_by") or []):
        _require(isinstance(o, dict), f"order_by[{i}] must be an object.")
        table = (o.get("table") or "").strip()
        col = (o.get("column") or "").strip()
        _require(table and col, f"order_by[{i}] needs a table and a column.")
        direction = str(o.get("direction") or "ASC").upper()
        _require(direction in DIRECTIONS, f"order_by[{i}] direction must be ASC or DESC.")
        order_by.append({"table": table, "column": col, "direction": direction})

    limit = spec.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise SpecError("limit must be an integer.")
        _require(limit > 0, "limit must be a positive integer.")

    return {
        "base_table": base,
        "columns": columns,
        "joins": joins,
        "filters": filters,
        "group_by": group_by,
        "having": having,
        "order_by": order_by,
        "distinct": bool(spec.get("distinct", False)),
        "limit": limit,
    }
