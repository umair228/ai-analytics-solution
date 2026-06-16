"""Auto-draft a semantic-layer YAML *skeleton* from live introspection — the
starting point a human then curates (descriptions, encodings, casts, joins,
metrics). Computes COUNT(*) + null_fraction per column (introspection doesn't
provide these) and guesses needs_cast / do_not_group for the curator to confirm.

    python manage.py build_semantic_layer --datasource EGPC_DEV --tables SAMPLE,RESULT,TEST
    python manage.py build_semantic_layer --datasource 1 --out semantics/specs/egpc_dev.draft.yaml

NOT a substitute for curation — the accuracy-defining content (encodings, the
real joins, metrics) is human-supplied. See DIS_TalkToData_Plan.md §4.
"""
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from sqlalchemy import text

from connections import introspection
from connections.engine import get_engine
from replication.sql_dialect import quote_ident, table_ref

from .load_semantic_layer import resolve_datasource

_NUMERIC_NAME = ("VALUE", "ENTRY", "NUMERIC", "MIN", "MAX", "AMOUNT", "QTY", "RESULT")


class Command(BaseCommand):
    help = "Draft a semantic-layer YAML skeleton from a datasource's live schema."

    def add_arguments(self, parser):
        parser.add_argument("--datasource", required=True, help="DataSource id or name.")
        parser.add_argument("--tables", default=None, help="Comma-separated subset.")
        parser.add_argument("--max-tables", type=int, default=25)
        parser.add_argument("--out", default=None, help="Output YAML path.")

    def handle(self, *args, **opts):
        ds = resolve_datasource(opts["datasource"])
        dialect = ds.source_type
        engine = get_engine(ds)

        all_tables = [t["name"] for t in introspection.list_tables(ds)]
        if opts["tables"]:
            want = {t.strip().upper() for t in opts["tables"].split(",")}
            tables = [t for t in all_tables if t.upper() in want]
        else:
            tables = all_tables[: opts["max_tables"]]
        if not tables:
            raise CommandError("No matching tables to draft.")

        spec = {"datasource": ds.name, "notes": "", "tables": {}, "joins": [], "metrics": []}
        for tname in tables:
            cols = introspection.list_columns(ds, tname)
            total, nulls = self._counts(engine, ds, tname, [c["name"] for c in cols], dialect)
            tdef = {"description": "", "synonyms": [], "columns": {}}
            if total is not None:
                tdef["row_count"] = total
            for c in cols:
                name = c["name"]
                # _counts returns non-null counts; convert to a null fraction.
                null_fraction = (None if total in (None, 0)
                                 else round(1 - nulls.get(name, 0) / total, 4))
                cdef = {"dtype": c.get("type", "")}
                if null_fraction is not None:
                    cdef["null_fraction"] = null_fraction
                    if null_fraction >= 0.9:
                        cdef["do_not_group"] = True
                if (any(k in name.upper() for k in _NUMERIC_NAME)
                        and "char" in str(c.get("type", "")).lower()):
                    cdef["needs_cast"] = True
                tdef["columns"][name] = cdef
            spec["tables"][tname] = tdef

        out = opts["out"] or (Path(settings.BASE_DIR) / "semantics" / "specs"
                              / f"{ds.name.lower().replace(' ', '_')}.draft.yaml")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
                             encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Wrote draft skeleton for {len(tables)} tables → {out}\n"
            "Now curate it (descriptions, encodings, joins, metrics) and "
            "`load_semantic_layer`."))

    def _counts(self, engine, ds, table, columns, dialect):
        """Return (total_rows, {col: non_null_count}). Best-effort; None on failure."""
        schema = ds.schema_name or ("dbo" if dialect == "mssql" else None)
        ref = table_ref(table, schema, dialect)
        select = ["COUNT(*) AS c_total"] + [
            f"COUNT({quote_ident(c, dialect)}) AS c_{i}"
            for i, c in enumerate(columns)]
        try:
            with engine.connect() as conn:
                row = conn.execute(text(f"SELECT {', '.join(select)} FROM {ref}")).fetchone()
        except Exception:  # noqa: BLE001
            return None, {}
        total = row[0]
        nulls = {c: row[i + 1] for i, c in enumerate(columns)}
        return total, nulls
