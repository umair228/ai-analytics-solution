"""
Optional pgvector backend for docsearch dense retrieval.

Used only when ``settings.DOCSEARCH_VECTOR_BACKEND == "pgvector"``. It stores one
row per indexed chunk — ``(ordinal, doc_id, embedding)`` — where ``ordinal`` is
the chunk's row position in the index DataFrame, so KNN results map straight back
to ``DocIndex.df`` rows. The numpy backend remains the default; this is the
drop-in for large corpora (millions of chunks) where an ANN index in Postgres
beats an in-memory matrix.

Requires: a Postgres reachable via ``settings.DOCSEARCH_PGVECTOR_DSN`` with the
``vector`` extension. psycopg2 is already a dependency. NOTE: this path needs a
live pgvector-enabled Postgres to exercise; the numpy backend is what the test
suite covers.
"""
from __future__ import annotations

from django.conf import settings


def _dsn() -> str:
    dsn = getattr(settings, "DOCSEARCH_PGVECTOR_DSN", "")
    if not dsn:
        raise RuntimeError(
            "DOCSEARCH_PGVECTOR_DSN is not set — required for the pgvector backend."
        )
    return dsn


def _table() -> str:
    # Table name comes from settings (not user input); used as an identifier.
    return getattr(settings, "DOCSEARCH_PGVECTOR_TABLE", "docsearch_chunk_vectors")


def _connect():
    import psycopg2
    return psycopg2.connect(_dsn())


def _vec_literal(vec) -> str:
    """pgvector text literal, e.g. '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{float(x):.6g}" for x in vec) + "]"


def ensure_table(dim: int):
    table = _table()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"  ordinal integer PRIMARY KEY,"
            f"  doc_id text,"
            f"  embedding vector({int(dim)})"
            f");"
        )
        # Cosine HNSW index for fast KNN (no-op if it already exists).
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_emb_hnsw "
            f"ON {table} USING hnsw (embedding vector_cosine_ops);"
        )
        conn.commit()


def replace_all(df, vectors):
    """Replace the whole table with the current chunk set (ordinal = row index)."""
    if vectors is None or len(vectors) == 0:
        return
    dim = int(vectors.shape[1])
    ensure_table(dim)
    table = _table()
    doc_ids = list(df["doc_id"]) if "doc_id" in df.columns else ["" for _ in range(len(vectors))]
    rows = [
        (i, str(doc_ids[i]) if i < len(doc_ids) else "", _vec_literal(vectors[i]))
        for i in range(len(vectors))
    ]
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table};")
        # executemany keeps it simple; for very large corpora switch to COPY.
        cur.executemany(
            f"INSERT INTO {table} (ordinal, doc_id, embedding) VALUES (%s, %s, %s);",
            rows,
        )
        conn.commit()


def truncate():
    """Clear all stored vectors (used when a rebuild produced no embeddings, so
    dense search degrades to empty rather than serving stale rows)."""
    table = _table()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table};")
        conn.commit()


def knn(qvec, pool: int):
    """Return [(ordinal, cosine_similarity), ...] for the top ``pool`` chunks."""
    table = _table()
    q = _vec_literal(qvec)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT ordinal, 1 - (embedding <=> %s::vector) AS sim "
            f"FROM {table} ORDER BY embedding <=> %s::vector LIMIT %s;",
            (q, q, int(pool)),
        )
        return [(int(o), float(s)) for o, s in cur.fetchall()]
