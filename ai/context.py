"""Builds a compact text description of a dataset for the LLM prompt."""
from analytics.engine import build_dataframe, column_statistics


def build_dataset_context(dataset, max_sample_rows: int = 15) -> str:
    """Describe a dataset (shape, statistics, sample rows) as plain text."""
    df = build_dataframe(dataset)
    stats = column_statistics(df)

    lines = [f"Dataset name: {dataset.name}"]
    if dataset.description:
        lines.append(f"Description: {dataset.description}")
    if dataset.query_id:
        lines.append(f"Underlying query: {dataset.query.name}")
    lines.append(f"Row count: {len(df)}")
    lines.append(f"Columns: {', '.join(str(c) for c in df.columns)}")
    lines.append("")
    lines.append("COLUMN STATISTICS:")
    for s in stats["statistics"]:
        if s["type"] == "numeric":
            lines.append(
                f"- {s['column']} (numeric): count={s['count']}, "
                f"min={s['min']}, max={s['max']}, mean={s['mean']}, "
                f"median={s['median']}, std={s['std']}, nulls={s['null_count']}"
            )
        else:
            tops = ", ".join(
                f"{t['value']} ({t['count']})"
                for t in s.get("top_values", [])[:5]
            )
            lines.append(
                f"- {s['column']} (text): distinct={s['distinct']}, "
                f"nulls={s['null_count']}, top values: {tops}"
            )

    lines.append("")
    lines.append(f"SAMPLE ROWS (first {max_sample_rows}):")
    columns = [str(c) for c in df.columns]
    lines.append(" | ".join(columns))
    for row in df.head(max_sample_rows).itertuples(index=False, name=None):
        lines.append(" | ".join("" if v is None else str(v) for v in row))

    return "\n".join(lines)
