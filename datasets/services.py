"""Dataset refresh — runs the underlying query and caches the result."""
from django.utils import timezone

from querybuilder.executor import execute_raw_sql, execute_spec
from querybuilder.models import QueryDefinition


def refresh_dataset(dataset):
    """Re-run the dataset's query and store the result on the dataset."""
    query = dataset.query
    datasource = query.datasource
    database = query.database or None

    if query.mode == QueryDefinition.Mode.RAW:
        result = execute_raw_sql(datasource, query.raw_sql, database)
    else:
        result = execute_spec(datasource, query.spec, database)

    dataset.cached_columns = result["columns"]
    dataset.cached_rows = result["rows"]
    dataset.row_count = result["row_count"]
    dataset.last_refreshed_at = timezone.now()
    dataset.last_error = ""
    dataset.save(
        update_fields=[
            "cached_columns", "cached_rows", "row_count",
            "last_refreshed_at", "last_error", "updated_at",
        ]
    )
    return result
