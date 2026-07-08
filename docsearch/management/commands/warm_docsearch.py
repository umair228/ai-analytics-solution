"""Pre-download and warm the AI Document Search models.

The Document Search query path loads the embedder and (optionally) the
cross-encoder reranker LAZILY on the first query — inside a gunicorn worker,
under the request timeout. On a CPU-only box the first query then has to download
(~0.5–1 GB each, HF-rate-limited without an HF_TOKEN) and load those models
synchronously, which can blow past the gunicorn timeout and kill the worker
(the client sees a 502). Run this ONCE after a deploy so the download + load
happens outside the request path (and, with HF_HOME on a persistent volume,
survives restarts):

    docker compose exec api python manage.py warm_docsearch

It respects DOCSEARCH_ENABLE_SEMANTIC / DOCSEARCH_ENABLE_RERANK, so it is a no-op
for whichever leg you have turned off.
"""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Download + warm the Document Search embedder and reranker models so "
            "the first query doesn't have to (and time out the gunicorn worker).")

    def handle(self, *args, **opts):
        from docsearch.retrieval import encode_query, get_embedder, get_reranker

        # Embedder (dense retrieval) --------------------------------------
        if getattr(settings, "DOCSEARCH_ENABLE_SEMANTIC", True):
            self.stdout.write("Loading embedder "
                              f"({getattr(settings, 'DOCSEARCH_EMBED_MODEL', '?')}) …")
            emb = get_embedder()
            if emb is not None:
                try:
                    encode_query("warmup")   # exercise the real encode path
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"  encode warmup skipped: {exc}"))
                self.stdout.write(self.style.SUCCESS("✓ embedder ready"))
            else:
                self.stdout.write(self.style.WARNING(
                    "embedder unavailable — Document Search will use BM25 only."))
        else:
            self.stdout.write("Semantic disabled (DOCSEARCH_ENABLE_SEMANTIC=False) — skipped.")

        # Reranker (optional cross-encoder) -------------------------------
        if getattr(settings, "DOCSEARCH_ENABLE_RERANK", True):
            self.stdout.write("Loading reranker "
                              f"({getattr(settings, 'DOCSEARCH_RERANK_MODEL', '?')}) …")
            rr = get_reranker()
            if rr is not None:
                try:
                    rr.predict([("warmup query", "warmup passage")])
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"  rerank warmup skipped: {exc}"))
                self.stdout.write(self.style.SUCCESS("✓ reranker ready"))
            else:
                self.stdout.write(self.style.WARNING(
                    "reranker unavailable — Document Search will use the fused order."))
        else:
            self.stdout.write(
                "Reranker disabled (DOCSEARCH_ENABLE_RERANK=False) — skipped. "
                "Recommended on a CPU-only box.")

        self.stdout.write(self.style.SUCCESS("\n✓ Document Search models warmed."))
