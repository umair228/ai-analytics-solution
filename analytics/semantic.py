"""Deterministic semantic query engine.

Turns a plain-English question into a real aggregation over a dataset.
spaCy parses the language (tokens + lemmas); the column/filter/aggregation
matching and the execution are fully deterministic and explainable — every
answer ships the exact interpretation it ran, so nothing is a black box.

This complements the Claude AI assistant: no API calls, no cost, and the
result is always reproducible.
"""
import re

import pandas as pd

# --------------------------------------------------------------------------
# spaCy is loaded lazily and cached; the engine degrades gracefully to a
# lightweight tokenizer if the model is not installed.
# --------------------------------------------------------------------------
_NLP = None
_NLP_STATE = "unloaded"  # unloaded | ready | unavailable


def _get_nlp():
    global _NLP, _NLP_STATE
    if _NLP_STATE != "unloaded":
        return _NLP
    try:
        import spacy

        _NLP = spacy.load("en_core_web_sm")
        _NLP_STATE = "ready"
    except Exception:  # noqa: BLE001 - any failure → fall back
        _NLP, _NLP_STATE = None, "unavailable"
    return _NLP


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------
AGGREGATION_WORDS = {
    "average": "mean", "avg": "mean", "mean": "mean",
    "count": "count", "number": "count", "tally": "count", "frequency": "count",
    "sum": "sum", "total": "sum", "combined": "sum", "aggregate": "sum",
    "max": "max", "maximum": "max", "highest": "max", "largest": "max",
    "peak": "max", "most": "max", "biggest": "max", "greatest": "max",
    "min": "min", "minimum": "min", "lowest": "min", "smallest": "min",
    "least": "min",
}
_AGG_LABEL = {
    "mean": "average", "count": "count", "sum": "total",
    "max": "maximum", "min": "minimum",
}
_AGG_PANDAS = {
    "mean": "mean", "count": "count", "sum": "sum", "max": "max", "min": "min",
}
_GROUP_TRIGGERS = {"by", "per", "across", "each"}
# Sub-tokens that are not distinctive enough to identify a column on their own.
_WEAK_SUBTOKENS = {
    "avg", "average", "mean", "sum", "total", "count", "num", "number",
    "id", "code", "of", "the", "by", "per", "value", "amount",
}
_DESC_WORDS = {"highest", "largest", "most", "top", "biggest", "greatest", "max",
               "maximum", "descending", "desc"}
_ASC_WORDS = {"lowest", "smallest", "least", "bottom", "min", "minimum",
              "ascending", "asc"}
_COMPARISONS = [
    (r"(?:greater than|more than|above|over|exceeding|exceeds|>=?)\s*([0-9][0-9,.]*)", "gt"),
    (r"(?:less than|fewer than|below|under|<=?)\s*([0-9][0-9,.]*)", "lt"),
    (r"(?:at least|minimum of)\s*([0-9][0-9,.]*)", "gte"),
    (r"(?:at most|maximum of|no more than)\s*([0-9][0-9,.]*)", "lte"),
    (r"(?:equal to|equals|exactly|=)\s*([0-9][0-9,.]*)", "eq"),
]


class SemanticError(Exception):
    """Raised when a question cannot be interpreted at all."""


# --------------------------------------------------------------------------
# Language parsing
# --------------------------------------------------------------------------
def _tokenize(question):
    """Return a list of {text, lemma} dicts for the question."""
    nlp = _get_nlp()
    if nlp is not None:
        return [
            {"text": t.text.lower(), "lemma": t.lemma_.lower()}
            for t in nlp(question)
            if not t.is_space and not t.is_punct
        ]
    tokens = []
    for word in re.findall(r"[A-Za-z0-9_.]+", question.lower()):
        lemma = word[:-1] if word.endswith("s") and len(word) > 3 else word
        tokens.append({"text": word, "lemma": lemma})
    return tokens


# --------------------------------------------------------------------------
# Dataset profiling
# --------------------------------------------------------------------------
def _subtokens(name):
    return [p for p in re.split(r"[^a-z0-9]+", str(name).lower()) if p]


def profile_dataset(df):
    """Classify columns and cache the categorical values for matching."""
    numeric, text, date = [], [], []
    value_index = {}  # column -> {UPPER value: original value}
    for col in df.columns:
        series = df[col]
        as_num = pd.to_numeric(series, errors="coerce")
        non_null = int(series.notna().sum())
        is_num = non_null > 0 and int(as_num.notna().sum()) >= non_null * 0.85

        name = str(col).lower()
        looks_dateish = bool(
            re.search(r"(date|_on$|_at$|month|day|time|year)", name)
        )
        is_date = False
        if not is_num and looks_dateish and non_null:
            parsed = pd.to_datetime(series, errors="coerce")
            is_date = int(parsed.notna().sum()) >= non_null * 0.7

        if is_date:
            date.append(col)
        elif is_num:
            numeric.append(col)
        else:
            text.append(col)
            uniques = series.dropna().astype(str).unique()
            if len(uniques) <= 500:
                value_index[col] = {v.upper(): v for v in uniques}

    return {
        "columns": list(df.columns),
        "numeric": numeric,
        "text": text,
        "date": date,
        "value_index": value_index,
        "subtokens": {c: _subtokens(c) for c in df.columns},
    }


# --------------------------------------------------------------------------
# Column / filter / aggregation detection
# --------------------------------------------------------------------------
def _score_column(col, meta, token_set, joined):
    """Score how strongly a question mentions a column (0 = no mention)."""
    subs = meta["subtokens"][col]
    core = [s for s in subs if s not in _WEAK_SUBTOKENS] or subs
    # Strong: the full column name appears as a contiguous phrase.
    name_variants = {
        "".join(subs), " ".join(subs), "_".join(subs), "".join(core),
    }
    score = 0
    if any(v and v in joined for v in name_variants):
        score = 3 + len(core)
    elif core and all(s in token_set for s in core):
        score = 2 + len(core)
    elif any(s in token_set for s in core):
        score = 1
    return score


def _detect_columns(tokens, meta):
    """Return {column: score} for every column the question mentions."""
    token_set = {t["text"] for t in tokens} | {t["lemma"] for t in tokens}
    joined = " ".join(t["text"] for t in tokens)
    joined_tight = joined.replace(" ", "")
    hits = {}
    for col in meta["columns"]:
        score = _score_column(col, meta, token_set, joined + " " + joined_tight)
        if score:
            hits[col] = score
    return hits


def _detect_aggregation(tokens, question_lower):
    if "how many" in question_lower or "how much" in question_lower:
        return "count"
    for t in tokens:
        for key in (t["lemma"], t["text"]):
            if key in AGGREGATION_WORDS:
                return AGGREGATION_WORDS[key]
    return None


def _column_in_window(window, col_hits, meta):
    """Best-scoring column whose sub-tokens appear in a window of words."""
    best, best_score = None, 0
    for col, score in col_hits.items():
        for sub in meta["subtokens"][col]:
            if sub in window and score > best_score:
                best, best_score = col, score
    return best


def _detect_group_by(tokens, col_hits, meta, has_limit):
    """Find the grouping column around a 'by'/'per'/'each' trigger.

    Usually the column *after* the trigger is the group. But in the
    "top N <X> by <Y>" pattern the group is the column *before* 'by'
    (Y is just the ranking measure) — so a limit flips the preference.
    """
    texts = [t["text"] for t in tokens]
    # Each position carries both its surface form and lemma so that plural
    # mentions ("labs") still match singular column names ("lab").
    forms = [{t["text"], t["lemma"]} for t in tokens]
    for i, word in enumerate(texts):
        if word not in _GROUP_TRIGGERS:
            continue
        after = set().union(set(), *forms[i + 1:i + 4])
        before = set().union(set(), *forms[max(0, i - 3):i])
        # date grouping: "by month" / "per year"
        if {"month", "monthly"} & after and meta["date"]:
            return ("__month__", meta["date"][0])
        if {"year", "yearly", "annually"} & after and meta["date"]:
            return ("__year__", meta["date"][0])

        after_col = _column_in_window(after, col_hits, meta)
        before_col = _column_in_window(before, col_hits, meta)
        if has_limit and before_col:
            return (before_col, before_col)
        if after_col:
            return (after_col, after_col)
        if before_col:
            return (before_col, before_col)
    return (None, None)


def _detect_filters(tokens, meta, used_columns):
    """Detect categorical (value-match) and numeric (comparison) filters."""
    filters = []
    texts = [t["text"] for t in tokens]
    n = len(texts)

    # Categorical: match question phrases against known column values.
    matched_spans = set()
    for size in (4, 3, 2, 1):
        for i in range(n - size + 1):
            if any((i + k) in matched_spans for k in range(size)):
                continue
            phrase = " ".join(texts[i:i + size]).upper()
            for col, values in meta["value_index"].items():
                if phrase in values:
                    filters.append({
                        "column": col, "op": "=", "value": values[phrase],
                    })
                    matched_spans.update(range(i, i + size))
                    break

    # Numeric: "<column> above 5", "turbidity over 3.2", etc.
    joined = " ".join(texts)
    for pattern, op in _COMPARISONS:
        for m in re.finditer(pattern, joined):
            raw = m.group(1).replace(",", "")
            try:
                number = float(raw)
            except ValueError:
                continue
            # attach to the numeric column mentioned closest before the match
            before = joined[:m.start()].split()
            target = None
            for col in meta["numeric"]:
                for sub in meta["subtokens"][col]:
                    if sub in before[-6:]:
                        target = col
            if target:
                filters.append({"column": target, "op": op, "value": number})
    return filters


def _detect_limit(tokens, question_lower):
    m = re.search(r"\b(?:top|bottom|first|last)\s+(\d+)", question_lower)
    if m:
        return int(m.group(1))
    return None


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
_OP_FUNCS = {
    "gt": lambda s, v: s > v, "lt": lambda s, v: s < v,
    "gte": lambda s, v: s >= v, "lte": lambda s, v: s <= v,
    "eq": lambda s, v: s == v,
}
_OP_LABEL = {"gt": ">", "lt": "<", "gte": "≥", "lte": "≤", "eq": "=", "=": "="}


def _apply_filters(df, filters):
    out = df
    for f in filters:
        col = f["column"]
        if col not in out.columns:
            continue
        if f["op"] == "=":
            out = out[out[col].astype(str).str.upper() == str(f["value"]).upper()]
        else:
            numeric = pd.to_numeric(out[col], errors="coerce")
            out = out[_OP_FUNCS[f["op"]](numeric, f["value"])]
    return out


def _round(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num != num:  # NaN
        return None
    return round(num, 2) if num % 1 else int(num)


def _humanize(name):
    return str(name).replace("_", " ")


def answer_question(df, question, dataset_name=""):
    """Interpret and execute a natural-language question over a dataframe."""
    question = (question or "").strip()
    if not question:
        raise SemanticError("Ask a question about the data.")
    if df is None or df.empty:
        raise SemanticError("This dataset has no rows to analyse.")

    tokens = _tokenize(question)
    q_lower = question.lower()
    meta = profile_dataset(df)

    col_hits = _detect_columns(tokens, meta)
    aggregation = _detect_aggregation(tokens, q_lower)
    limit = _detect_limit(tokens, q_lower)
    group_kind, group_col = _detect_group_by(tokens, col_hits, meta, bool(limit))

    group_real = group_col if group_kind not in ("__month__", "__year__") else group_kind
    used = {group_real} if group_real else set()
    filters = _detect_filters(tokens, meta, used)
    filter_cols = {f["column"] for f in filters}

    # Measure = the best-scoring numeric column that is not the group key.
    measure = None
    numeric_hits = {
        c: s for c, s in col_hits.items()
        if c in meta["numeric"] and c != group_real and c not in filter_cols
    }
    if numeric_hits:
        measure = max(numeric_hits, key=numeric_hits.get)

    if aggregation is None:
        aggregation = "mean" if measure else "count"
    if aggregation != "count" and measure is None:
        # asked for sum/avg/max but no numeric column resolved
        raise SemanticError(
            f"I understood you want a {_AGG_LABEL[aggregation]}, but couldn't "
            f"tell which numeric column to {_AGG_LABEL[aggregation]}. "
            f"Numeric columns: {', '.join(_humanize(c) for c in meta['numeric']) or 'none'}."
        )

    sort_desc = not bool(_ASC_WORDS & {t["text"] for t in tokens}) or bool(
        _DESC_WORDS & {t["text"] for t in tokens}
    )

    working = _apply_filters(df, filters)
    matched = int(len(working))

    if group_kind == "__month__":
        group_display = "month"
    elif group_kind == "__year__":
        group_display = "year"
    elif group_real:
        group_display = _humanize(group_real)
    else:
        group_display = None

    interpretation = {
        "aggregation": aggregation,
        "aggregation_label": _AGG_LABEL[aggregation],
        "measure": str(measure) if measure else None,
        "group_by": group_display,
        "group_kind": "date" if group_kind in ("__month__", "__year__") else
        ("column" if group_real else None),
        "filters": [
            {"column": _humanize(f["column"]), "op": _OP_LABEL.get(f["op"], f["op"]),
             "value": f["value"]}
            for f in filters
        ],
        "sort": "desc" if sort_desc else "asc",
        "limit": limit,
        "engine": "spaCy" if _NLP_STATE == "ready" else "lightweight",
    }

    filter_phrase = ""
    if filters:
        parts = [
            f"{_humanize(f['column'])} {_OP_LABEL.get(f['op'], f['op'])} {f['value']}"
            for f in filters
        ]
        filter_phrase = " where " + " and ".join(parts)

    # ---- Grouped result -------------------------------------------------
    if group_real:
        if working.empty:
            return _empty_answer(question, interpretation, filter_phrase)
        if group_kind == "__month__":
            keys = pd.to_datetime(working[group_col], errors="coerce").dt.strftime("%Y-%m")
            group_label = "month"
        elif group_kind == "__year__":
            keys = pd.to_datetime(working[group_col], errors="coerce").dt.strftime("%Y")
            group_label = "year"
        else:
            keys = working[group_real].fillna("(blank)").astype(str)
            group_label = _humanize(group_real)

        if aggregation == "count":
            series = keys.groupby(keys).size()
            value_label = "count"
        else:
            values = pd.to_numeric(working[measure], errors="coerce")
            series = getattr(values.groupby(keys), _AGG_PANDAS[aggregation])()
            value_label = f"{_AGG_LABEL[aggregation]} {_humanize(measure)}"

        is_time = group_kind in ("__month__", "__year__")
        series = series.sort_index() if is_time else series.sort_values(ascending=not sort_desc)
        if limit:
            series = series.head(limit)

        rows = [[str(k), _round(v)] for k, v in series.items()]
        top = rows[0] if rows else None
        answer = (
            f"{value_label.capitalize()} by {group_label}{filter_phrase}: "
            f"{len(rows)} groups"
        )
        if top and not is_time:
            extreme = "Highest" if sort_desc else "Lowest"
            answer += f". {extreme} is {top[0]} ({top[1]:,})." if isinstance(
                top[1], (int, float)
            ) else f". {extreme} is {top[0]}."
        chart_type = "line" if is_time else ("pie" if len(rows) <= 6
                                             and aggregation == "count" else "bar")
        return {
            "question": question,
            "understood": True,
            "answer": answer,
            "interpretation": interpretation,
            "result": {"columns": [group_label, value_label], "rows": rows},
            "scalar": None,
            "rows_matched": matched,
            "chart": {
                "type": chart_type,
                "category_field": group_label,
                "value_field": value_label,
            },
        }

    # ---- Scalar result --------------------------------------------------
    if aggregation == "count":
        scalar = matched
        answer = (
            f"{scalar:,} row{'' if scalar == 1 else 's'}{filter_phrase or ' in total'}."
        )
        value_label = "count"
    else:
        if working.empty:
            return _empty_answer(question, interpretation, filter_phrase)
        values = pd.to_numeric(working[measure], errors="coerce").dropna()
        scalar = _round(getattr(values, _AGG_PANDAS[aggregation])())
        value_label = f"{_AGG_LABEL[aggregation]} {_humanize(measure)}"
        shown = f"{scalar:,}" if isinstance(scalar, (int, float)) else scalar
        answer = (
            f"The {_AGG_LABEL[aggregation]} {_humanize(measure)}{filter_phrase} "
            f"is {shown}."
        )

    return {
        "question": question,
        "understood": True,
        "answer": answer,
        "interpretation": interpretation,
        "result": {"columns": [value_label], "rows": [[scalar]]},
        "scalar": scalar,
        "rows_matched": matched,
        "chart": {"type": "kpi", "category_field": None, "value_field": value_label},
    }


def _empty_answer(question, interpretation, filter_phrase):
    return {
        "question": question,
        "understood": True,
        "answer": f"No rows match{filter_phrase or ' your question'}.",
        "interpretation": interpretation,
        "result": {"columns": [], "rows": []},
        "scalar": 0,
        "rows_matched": 0,
        "chart": {"type": None},
    }


# --------------------------------------------------------------------------
# Question suggestions
# --------------------------------------------------------------------------
def suggest_questions(df, dataset_name=""):
    """Generate a handful of example questions tailored to the dataset."""
    if df is None or df.empty:
        return []
    meta = profile_dataset(df)
    cats = [c for c in meta["text"] if 1 < df[c].nunique() <= 60]
    cats.sort(key=lambda c: df[c].nunique())
    numerics = meta["numeric"]
    dates = meta["date"]

    suggestions = ["How many rows are there in total?"]
    if cats:
        suggestions.append(f"Count of rows by {_humanize(cats[0])}")
    if numerics and cats:
        suggestions.append(
            f"Average {_humanize(numerics[0])} by {_humanize(cats[0])}"
        )
    if numerics:
        suggestions.append(f"What is the maximum {_humanize(numerics[0])}?")
    if numerics and dates:
        suggestions.append(f"Show {_humanize(numerics[0])} by month")
    if len(cats) > 1 and numerics:
        suggestions.append(
            f"Highest {_humanize(numerics[-1])} by {_humanize(cats[1])}"
        )
    # a filtered example using a real categorical value
    if cats and meta["value_index"].get(cats[0]):
        sample_value = next(iter(meta["value_index"][cats[0]].values()))
        suggestions.append(f"How many rows have {_humanize(cats[0])} {sample_value}?")
    return suggestions[:6]
