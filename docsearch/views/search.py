"""
Main AI Document Search endpoint.

    POST /api/doc-search/
    {
      "question": "How does inventory management work in LabWare?",
      "mode": "auto" | "docs" | "data",     # optional, default "auto"
      "history": [{"role":"user"|"assistant","content":"..."}]  # optional, for follow-ups
    }
    ->
    {
      "question": "...", "mode": "...",
      "answer": "...", "sources": [{"doc","page","label"}], "engine": "claude|extractive|faq|none",
      "sql_result": {"sql","columns","rows","row_count"},   # present only for data questions
      "retrieval": {"best_score","semantic_top_sim"}
    }

Mirrors the forecasting views' graceful-degradation contract: never returns 5xx
for a normal query — partial failures are reported inside a 200 payload.
"""
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..answer import synthesize_answer
from ..faq import faq_lookup
from ..index_store import get_index
from ..nlp_sql import run_nl_sql


class DocSearchView(APIView):
    def post(self, request):
        data = request.data or {}
        question = (data.get("question") or "").strip()
        if not question:
            return Response({"error": "Missing field: question."}, status=status.HTTP_400_BAD_REQUEST)

        mode = (data.get("mode") or "auto").lower()
        if mode not in ("auto", "docs", "data"):
            mode = "auto"
        history = data.get("history") if isinstance(data.get("history"), list) else []

        resp = {"question": question, "mode": mode, "sources": []}

        # 1) FAQ fast-path (greetings / canned LabWare facts) — docs/auto only.
        if mode in ("auto", "docs"):
            try:
                faq = faq_lookup(question)
            except Exception as exc:
                print(f"[doc-search] faq error: {exc}")
                faq = None
            if faq and faq["kind"] in ("exact", "fuzzy"):
                resp.update({"answer": faq["answer"], "engine": "faq", "faq": True})
                return Response(resp, status=status.HTTP_200_OK)

        # 2) Document retrieval + answer synthesis (docs/auto).
        if mode in ("auto", "docs"):
            try:
                rows, debug = get_index().retrieve(question, top_k=10, expand_neighbors=2)
                ans = synthesize_answer(question, rows, history=history)
                resp.update({
                    "answer": ans["answer"],
                    "sources": ans["sources"],
                    "engine": ans["engine"],
                    "retrieval": {
                        "best_score": round(float(debug.get("best_score", 0.0)), 4),
                        "semantic_top_sim": round(float(debug.get("semantic_top_sim", 0.0)), 4),
                    },
                })
            except Exception as exc:
                print(f"[doc-search] retrieval error: {exc}")
                resp.setdefault("answer", "Document search is temporarily unavailable.")
                resp.setdefault("engine", "error")

        # 3) NL->SQL over the live LIMS DB (data/auto).
        if getattr(settings, "DOCSEARCH_ENABLE_SQL", True) and mode in ("auto", "data"):
            try:
                sql_result = run_nl_sql(question)
                if sql_result:
                    resp["sql_result"] = sql_result
            except Exception as exc:
                print(f"[doc-search] nl-sql error: {exc}")
                resp["sql_error"] = "The data query could not be executed."

        # data-only: provide a sensible answer string around the table.
        if mode == "data" and "answer" not in resp:
            if "sql_result" in resp:
                resp["answer"] = f"Returned {resp['sql_result']['row_count']} row(s) from the LIMS database."
                resp["engine"] = "sql"
            else:
                resp["answer"] = (
                    "That doesn't look like a structured-data question I can turn into a query. "
                    "Try e.g. \"sample status\", \"requests created by F_ALSHAMILI\", "
                    "or \"requests with template veterinary\"."
                )
                resp["engine"] = "none"
        elif "sql_result" in resp and resp.get("engine") in ("none", "error"):
            resp["answer"] = f"Returned {resp['sql_result']['row_count']} row(s) from the LIMS database."
            resp["engine"] = "sql"

        return Response(resp, status=status.HTTP_200_OK)
