"""Materialize file data sources (Excel / CSV) into a SQLite database so the
visual query builder can run real SQL against files, uniformly with databases.
"""
import re

import pandas as pd
from django.conf import settings
from sqlalchemy import create_engine

from .constants import SourceType


def safe_table_name(name: str) -> str:
    cleaned = re.sub(r"\W+", "_", str(name).strip()).strip("_")
    return cleaned or "sheet"


def materialize_file(datasource, file_path) -> tuple[str, list[str]]:
    """Load an uploaded file into a SQLite DB. Each Excel sheet becomes a
    table; a CSV becomes a single ``data`` table. Returns (sqlite_path, tables).
    """
    settings.DSE_MATERIALIZED_DIR.mkdir(parents=True, exist_ok=True)
    target = settings.DSE_MATERIALIZED_DIR / f"ds_{datasource.id}.sqlite-materialized"
    if target.exists():
        target.unlink()

    engine = create_engine(f"sqlite:///{target}")
    tables: list[str] = []
    try:
        if datasource.source_type == SourceType.CSV:
            frame = pd.read_csv(file_path)
            frame.to_sql("data", engine, index=False, if_exists="replace")
            tables.append("data")
        else:
            sheets = pd.read_excel(file_path, sheet_name=None)
            used: set[str] = set()
            for sheet_name, frame in sheets.items():
                name = safe_table_name(sheet_name)
                base, counter = name, 1
                while name in used:
                    name = f"{base}_{counter}"
                    counter += 1
                used.add(name)
                frame.to_sql(name, engine, index=False, if_exists="replace")
                tables.append(name)
    finally:
        engine.dispose()

    return str(target), tables
