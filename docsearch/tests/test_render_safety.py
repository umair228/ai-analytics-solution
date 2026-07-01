"""
Regression test for the AI Document Search 500 that appeared *after uploading a
document* (production symptom: the frontend showed "Request failed with status
code 500" on an otherwise normal query).

Root cause: a page-less passage (``page=None``) mixed with paged ones makes the
pandas ``page`` column ``float64`` with ``NaN``. ``answer._unique_sources`` copied
that ``numpy.float64('nan')`` straight into the response ``sources``. DRF's
``JSONRenderer`` renders with ``allow_nan=False``, so the ``NaN`` raised
``ValueError: Out of range float values are not JSON compliant`` at *render* time
— a 500 thrown OUTSIDE ``DocSearchView``'s try/except, defeating its documented
"never returns 5xx for a normal query" contract.

The endpoint must now degrade gracefully to a 200 with strictly JSON-safe sources.

Run just this module:
    DJANGO_SETTINGS_MODULE=config.settings.dev DB_ENGINE=sqlite \
    DOCSEARCH_DB_ENGINE=sqlite \
    python manage.py test docsearch.tests.test_render_safety -v2
"""
import json
import math
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from docsearch import index_store, ingest
from docsearch.answer import _clean_page, _unique_sources
from docsearch.models import KnowledgeRecord

User = get_user_model()

# Distinctive, non-stopword tokens so BM25 retrieval is deterministic about which
# document is the top hit (semantic rerank is disabled below).
PAGED_TOKEN = "zzpagedtoken"
PAGELESS_TOKEN = "zzpagelesstoken"


def _iter_floats(obj):
    """Yield every float found anywhere in a JSON-decoded structure."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_floats(v)
    elif isinstance(obj, float):
        yield obj


class DocsearchRenderSafetyTestCase(APITestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docsearch-render-")
        corpus = os.path.join(self.tmp, "corpus")
        os.makedirs(corpus, exist_ok=True)
        self._override = override_settings(
            MEDIA_ROOT=self.tmp,
            DOCSEARCH_CORPUS_DIR=corpus,
            DOCSEARCH_INDEX_PATH=os.path.join(self.tmp, "chunks_index.csv"),
            DOCSEARCH_STAGING_DIR=os.path.join(self.tmp, "staging"),
            DOCSEARCH_VECTORS_PATH=os.path.join(self.tmp, "chunk_vectors.npy"),
            DOCSEARCH_FAQ_PATH=os.path.join(self.tmp, "no_such_faq.xlsx"),
            DOCSEARCH_ENABLE_SEMANTIC=False,
            DOCSEARCH_ENABLE_RERANK=False,
            DOCSEARCH_ENABLE_SQL=False,
        )
        self._override.enable()
        index_store._INDEX = None  # process-global singleton -> reset for isolation
        self.user = User.objects.create_user(username="ana", password="pw", role="analyst")

    def tearDown(self):
        index_store._INDEX = None
        self._override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approve(self, doc_id, passages):
        rec = ingest._stage(
            source_type=KnowledgeRecord.Source.DOCUMENT,
            doc_id=doc_id,
            passages=passages,
            created_by=self.user,
        )
        ingest.approve_record(rec, reviewed_by=self.user)  # APPROVED + reindex
        return rec

    def test_pageless_chunk_does_not_500_the_search(self):
        # One paged doc + one page-less doc => the merged 'page' column is float64
        # with NaN (exactly what an upload rebuild produces on a mixed corpus).
        self._approve("paged.docx", [{"page": 1, "text":
            f"Calibration overview {PAGED_TOKEN} bravo charlie delta echo foxtrot."}])
        self._approve("pageless.docx", [{"page": None, "text":
            f"Calibration detail {PAGELESS_TOKEN} bravo charlie delta echo foxtrot."}])

        # Sanity: the index really does carry a NaN page (else the test is vacuous).
        df = index_store.get_index().df
        self.assertTrue(df["page"].isna().any(),
                        "expected a NaN page in the index to exercise the regression")

        self.client.force_authenticate(self.user)
        resp = self.client.post(
            reverse("doc-search"),
            {"question": f"tell me about {PAGELESS_TOKEN}", "mode": "docs"},
            format="json",
        )

        # The whole point: a normal query must not 500 anymore.
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content[:500])

        # And the rendered body must be strictly JSON-safe (no NaN/Inf anywhere).
        body = json.loads(resp.content)
        for f in _iter_floats(body):
            self.assertTrue(math.isfinite(f), f"non-finite float leaked into response: {f!r}")

        # The page-less source is present and its page is a clean JSON value.
        for s in body.get("sources", []):
            self.assertNotIsInstance(s.get("page"), float,
                                     "page should be coerced away from raw float NaN")

    def test_clean_page_unit(self):
        self.assertEqual(_clean_page(float("nan")), "?")
        self.assertEqual(_clean_page(float("inf")), "?")
        self.assertEqual(_clean_page(3.0), 3)
        self.assertIsInstance(_clean_page(3.0), int)
        self.assertEqual(_clean_page(3), 3)
        self.assertEqual(_clean_page("image"), "image")

    def test_unique_sources_never_emits_nan(self):
        import numpy as np
        import pandas as pd

        df = pd.DataFrame(
            [{"doc_id": "d.docx", "page": 1}, {"doc_id": "d.docx", "page": None}]
        )  # mixed int + None -> float64 NaN
        rows = [df.iloc[i] for i in range(len(df))]
        for s in _unique_sources(rows):
            self.assertFalse(isinstance(s["page"], float) and math.isnan(s["page"]))
