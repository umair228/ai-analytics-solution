-- Enable the pgvector extension on the DIS metadata database (runs once on first
-- container init). Required when DOCSEARCH_VECTOR_BACKEND=pgvector.
CREATE EXTENSION IF NOT EXISTS vector;
