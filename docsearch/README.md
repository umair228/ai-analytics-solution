# docsearch — AI Document Search

RAG-style question answering over LabWare LIMS knowledge documents, plus
natural-language → SQL over the live LIMS database. Ported from the LW-Chatbot
Flask app into a Django REST app that mirrors the `forecasting` app conventions
(class-based `APIView`, no ORM, app-local `db.py`/`urls.py`).

## What it does

1. **Document Q&A (RAG).** Documents in the corpus are extracted into
   sentence-level chunks and indexed with **BM25** (keyword scoring + title /
   keyword / entity / intent boosts). At query time the top chunks are optionally
   re-ranked with a **MiniLM** sentence embedder, then handed to the shared DSE
   **Claude** client (`ai.client.run_chat`) which writes a concise, **cited**
   answer. If `CLAUDE_API_KEY` is unset, it falls back to an offline **extractive**
   answer (top relevant sentences).
2. **Natural-language → SQL.** Recognised "data" questions (e.g. *sample status*,
   *requests created by F_ALSHAMILI*, *requests with template veterinary*) are
   parsed into SQL and run against the live LIMS DB (`DOCSEARCH_DB`, MSSQL by
   default). The metadata is mapped to the **real SMJMUN_DEV schema**
   (`v_lw_nlp_bot`, `SAMPLE`/`TEST`/`RESULT`).
3. **Corpus management.** List / upload / delete documents; the index rebuilds
   automatically on change.

## Endpoints (mounted under `/api/`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/doc-search/` | Ask a question (`{question, mode?, history?}`) |
| GET | `/api/doc-search/status/` | Feature config + index summary |
| GET | `/api/doc-search/documents/` | List indexed documents |
| POST | `/api/doc-search/documents/` | Upload documents (multipart `files`) |
| DELETE | `/api/doc-search/documents/<name>/` | Delete a document |

`mode` is `auto` (docs + data, default), `docs` (RAG only) or `data` (NL→SQL only).

## Modules

- `text_extract.py` — docx / pptx / pdf / txt / image(OCR) → chunks
- `retrieval.py` — BM25 + boosts + optional semantic rerank (`DocIndex`)
- `index_store.py` — build / persist / load the chunk index; document listing
- `answer.py` — Claude RAG synthesis (+ extractive fallback)
- `faq.py` — optional canned replies (`standard_responses.xlsx`)
- `nlp_sql.py` + `db.py` — NL→SQL over the LIMS DB (schema-verified)
- `views/` — `search`, `documents`, `status`
- `management/commands/build_doc_index.py` — `--seed` then reindex

## Setup

```bash
# deps (kept compatible with NeuralProphet's numpy<2 / torch pins)
uv pip install -c .docsearch-constraints.txt python-docx python-pptx pypdf sentence-transformers

# seed the starter corpus (LW-Chatbot LIMS docs) and build the index
python manage.py build_doc_index --seed
```

## Config (`.env` / settings)

`DOCSEARCH_DB_ENGINE` (default `mssql`), `DOCSEARCH_ENABLE_SEMANTIC`,
`DOCSEARCH_ENABLE_OCR`, `DOCSEARCH_ENABLE_SQL`, `DOCSEARCH_DB_TIMEOUT`,
`DOCSEARCH_CORPUS_DIR`, `DOCSEARCH_INDEX_PATH`, `DOCSEARCH_FAQ_PATH`,
`DOCSEARCH_EMBED_MODEL`. The NL→SQL DB reuses `DB_*` credentials.

> Note: the original PPTX corpus shipped as un-hydrated git-LFS pointer stubs, so
> the indexed corpus is the 20 real `.docx` LIMS training documents (plus any
> uploads). The first semantic query downloads the MiniLM weights from HuggingFace.
