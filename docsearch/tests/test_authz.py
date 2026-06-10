"""
Model-independent authorization / data-leakage regression tests for docsearch.

These are deliberately written BEFORE any production fix, to *prove the current
horizontal-authorization gaps exist* (most assertions are expected to FAIL on the
current code — they are "red" tests). They encode only invariants that hold under
EVERY candidate tenancy model (owner/shared, org/site/lab, per-document ACL,
per-tenant index): namely, that an actor with **no relationship of any kind** to a
resource (different user, role=viewer, not owner, not shared, not admin) must not
be able to read it or mutate the shared corpus.

Assertion discipline (important): the search question / agent query IS the unique
TOKEN, and the API echoes it back (``question`` / ``query``). So we assert TOKEN /
DOC_ID are absent ONLY from leak-bearing fields (answer, sources/citations,
serialized KB records, passages, doc_id) — never from the echoed input. A
whole-response check would be self-defeating and could never go green.

Temporary slice policy assumed by these tests (NOT asserting final tenancy):
  * admin can see everything (not exercised here);
  * a KnowledgeRecord is private to ``created_by`` unless explicitly made global;
  * legacy corpus FILES are treated as global-readable, but viewers must not
    upload/delete corpus content.

Out of scope on purpose (do NOT add here): same-org visibility, shared-user
grants, per-document ACL grants, final tenancy behaviour, and NL->SQL tenancy.

Run just this module:
    DJANGO_SETTINGS_MODULE=config.settings.dev DB_ENGINE=sqlite \
    DOCSEARCH_DB_ENGINE=sqlite \
    python manage.py test docsearch.tests.test_authz -v2
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from ai.tools import execute_tool
from connections.models import DataSource
from datasets.models import Dataset
from docsearch import index_store, ingest
from docsearch.models import KnowledgeRecord
from querybuilder.models import QueryDefinition

User = get_user_model()

# A unique, non-stopword token that only ever appears inside Alice's private
# content. If it shows up in another user's LEAK-BEARING fields, content leaked.
TOKEN = "zztokensecret"
DOC_ID = "alice_private_sop.docx"
PASSAGE = f"Confidential laboratory procedure alpha {TOKEN} bravo charlie delta echo."
DATASET_SECRET = "datasetsecretvalue"

# Denial codes accepted as "secure" where a whole endpoint may validly refuse.
DENIED = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


class DocsearchAuthzTestCase(APITestCase):
    """Shared fixtures: isolated temp MEDIA/index, semantic/rerank/SQL off, and a
    fresh global index per test. Three unrelated users (two analysts + a viewer)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="docsearch-authz-")
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
        # The index is a process-global singleton — reset it so cases are isolated.
        index_store._INDEX = None

        # Alice authors private content; Bob is an unrelated builder; Vic is a
        # lowest-privilege viewer. None of them share anything.
        self.alice = User.objects.create_user(username="alice", password="pw", role="analyst")
        self.bob = User.objects.create_user(username="bob", password="pw", role="analyst")
        self.viewer = User.objects.create_user(username="vic", password="pw", role="viewer")

    def tearDown(self):
        index_store._INDEX = None
        self._override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- helpers -----
    def _stage_pending(self, user, doc_id=DOC_ID, text=PASSAGE) -> KnowledgeRecord:
        """Create a PENDING KnowledgeRecord owned by ``user`` (not indexed)."""
        return ingest._stage(
            source_type=KnowledgeRecord.Source.DOCUMENT,
            doc_id=doc_id,
            passages=[{"page": 1, "text": text}],
            created_by=user,
        )

    def _approve_doc(self, user, doc_id=DOC_ID, text=PASSAGE) -> KnowledgeRecord:
        """Stage + approve a doc owned by ``user`` (rebuilds the search index)."""
        rec = self._stage_pending(user, doc_id, text)
        ingest.approve_record(rec, reviewed_by=user)  # flips to APPROVED + reindex
        return rec

    def _private_dataset(self, owner, secret=DATASET_SECRET) -> Dataset:
        """Minimal owned Dataset with cached rows (not shared with anyone)."""
        ds = DataSource.objects.create(owner=owner, name="alice-src", source_type="sqlite")
        qd = QueryDefinition.objects.create(owner=owner, name="alice-query", datasource=ds)
        return Dataset.objects.create(
            owner=owner,
            name="Alice secret dataset",
            query=qd,
            cached_columns=["secret_col"],
            cached_rows=[{"secret_col": secret}],
            row_count=1,
        )

    def _corpus_files(self):
        from django.conf import settings
        d = settings.DOCSEARCH_CORPUS_DIR
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    @staticmethod
    def _dump(data):
        return json.dumps(data, default=str)


class RetrievalLeakageTests(DocsearchAuthzTestCase):
    """[RED] Cross-user retrieval through the public endpoint and the agent tool."""

    def test_search_excludes_other_users_kb_content(self):
        """[RED] An unrelated VIEWER must not retrieve Alice's approved KB content
        via POST /api/doc-search/. Fails today: the index is global and retrieval
        takes no user (docsearch/views/search.py:60, retrieval.py:393)."""
        self._approve_doc(self.alice)

        self.client.force_authenticate(self.viewer)
        resp = self.client.post(
            reverse("doc-search"), {"question": TOKEN, "mode": "docs"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Assert ONLY on leak-bearing fields — never the echoed `question`/`mode`
        # (the question IS the token, so a whole-body check is self-defeating).
        answer = resp.data.get("answer") or ""
        sources_blob = self._dump(resp.data.get("sources") or [])
        retrieval_blob = self._dump(resp.data.get("retrieval") or {})
        self.assertNotIn(TOKEN, answer)
        self.assertNotIn(DOC_ID, answer)
        self.assertNotIn(DOC_ID, sources_blob)
        self.assertNotIn(TOKEN, sources_blob)
        self.assertNotIn(DOC_ID, retrieval_blob)

    def test_agent_search_documents_excludes_other_users_kb_content(self):
        """[RED] The agent's ``search_documents`` tool must apply the same scope.
        Fails today: ai/tools.py:_search_documents ignores its ``user`` argument
        and calls the global index (ai/tools.py:288)."""
        self._approve_doc(self.alice)

        result = execute_tool("search_documents", self.viewer, {"query": TOKEN})
        self.assertNotIn("error", result, f"tool errored instead of returning a result: {result}")

        # Assert ONLY on leak-bearing fields — never the echoed `query`.
        for p in result.get("passages", []):
            self.assertNotIn(TOKEN, str(p.get("excerpt", "")))
            self.assertNotIn(DOC_ID, str(p.get("source", "")))


class KnowledgeIngestAuthzTests(DocsearchAuthzTestCase):
    """[RED] Dataset->KB ingest must enforce dataset access (IDOR)."""

    def test_dataset_ingest_requires_dataset_access(self):
        """[RED] An unrelated BUILDER (Bob) must not ingest Alice's private dataset
        by id. Fails today: KnowledgeIngestDatasetView never calls
        ``dataset.accessible_by`` (docsearch/views/knowledge.py:137)."""
        dataset = self._private_dataset(self.alice)

        self.client.force_authenticate(self.bob)  # analyst/can_build, NOT the owner
        resp = self.client.post(
            reverse("kb-ingest-datasets"), {"dataset": dataset.id}, format="json"
        )
        # No-access must be denied (403) or hidden (404); never a successful ingest.
        self.assertIn(resp.status_code, DENIED)
        self.assertFalse(
            KnowledgeRecord.objects.filter(dataset=dataset).exists(),
            "Bob ingested Alice's dataset into the knowledge base (IDOR).",
        )


class KnowledgeDisclosureTests(DocsearchAuthzTestCase):
    """[RED] Staged records and indexed sources must not leak across users."""

    def test_kb_list_hides_other_users_pending_record(self):
        """[RED] A viewer must not see Alice's pending record in the KB list.
        Secure outcomes: deny the whole endpoint (403/404), OR return 200 without
        Alice's record. Fails today: get_queryset returns
        ``KnowledgeRecord.objects.all()`` (docsearch/views/knowledge.py:61)."""
        rec = self._stage_pending(self.alice)

        self.client.force_authenticate(self.viewer)
        resp = self.client.get(reverse("kb-list"))
        if resp.status_code in DENIED:  # wholesale denial is a secure outcome
            return
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.data
        items = data["results"] if isinstance(data, dict) and "results" in data else data
        ids = [r.get("id") for r in items]
        self.assertNotIn(rec.id, ids, "Alice's staged record is visible to an unrelated viewer.")
        self.assertNotIn(DOC_ID, self._dump(items))

    def test_kb_detail_hides_other_users_pending_record(self):
        """[RED] A viewer must not read Alice's pending record detail (incl. its
        passages). Fails today: KnowledgeDetailView._get is unscoped
        (docsearch/views/knowledge.py:47, 163)."""
        rec = self._stage_pending(self.alice)

        self.client.force_authenticate(self.viewer)
        resp = self.client.get(reverse("kb-detail", kwargs={"pk": rec.id}))
        self.assertIn(resp.status_code, DENIED)
        # On a secure denial there is no payload to leak; this also guards against a
        # 200 that returns the record's passages.
        self.assertNotIn(TOKEN, self._dump(resp.data))
        self.assertNotIn(DOC_ID, self._dump(resp.data))

    def test_kb_sources_hide_other_users_private_doc(self):
        """[RED] Source listing must not reveal another user's private indexed
        doc_id. Secure outcomes: deny the whole endpoint (403/404), OR return 200
        without Alice's doc_id. Fails today: list_indexed_sources is global
        (docsearch/views/knowledge.py:213, index_store.py:245)."""
        self._approve_doc(self.alice)

        self.client.force_authenticate(self.viewer)
        resp = self.client.get(reverse("kb-sources"))
        if resp.status_code in DENIED:  # wholesale denial is a secure outcome
            return
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(DOC_ID, self._dump(resp.data))


class CorpusMutationAuthzTests(DocsearchAuthzTestCase):
    """[RED] Legacy corpus endpoints must not let a viewer mutate shared content.
    The security invariant is NO file created/deleted by an unauthorized user."""

    def test_viewer_cannot_upload_corpus_document(self):
        """[RED] A viewer must not upload into the corpus. Fails today:
        DocumentsView has no role gate (docsearch/views/documents.py:39)."""
        before = self._corpus_files()
        upload = SimpleUploadedFile(
            "viewer_evil.txt", b"viewer injected payload text content here", content_type="text/plain"
        )
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(
            reverse("doc-search-documents"), {"files": upload}, format="multipart"
        )
        # Mutation invariant first: the corpus must be unchanged regardless of code.
        self.assertEqual(self._corpus_files(), before, "Viewer mutated the corpus (uploaded a file).")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_cannot_delete_corpus_document(self):
        """[RED] A viewer must not delete a corpus document. Fails today:
        DocumentDeleteView has no role gate (docsearch/views/documents.py:96)."""
        from django.conf import settings
        seed = os.path.join(settings.DOCSEARCH_CORPUS_DIR, "seed.txt")
        with open(seed, "w") as fh:
            fh.write("seed corpus document text content here")

        self.client.force_authenticate(self.viewer)
        resp = self.client.delete(
            reverse("doc-search-document-delete", kwargs={"name": "seed.txt"})
        )
        # Mutation invariant first: the file must still exist regardless of code.
        self.assertTrue(os.path.exists(seed), "Viewer deleted a corpus document.")
        self.assertIn(resp.status_code, DENIED)


class OwnerPositiveGuardTests(DocsearchAuthzTestCase):
    """[GUARD] Positive control — not a red test. Protects against a lazy
    "deny everyone" fix. Allowed to pass today (no scoping → owner sees own)."""

    def test_owner_can_retrieve_own_kb_content(self):
        """[GUARD] The author (Alice) SHOULD still retrieve her own approved
        content after scoping is added."""
        self._approve_doc(self.alice)

        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            reverse("doc-search"), {"question": TOKEN, "mode": "docs"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Positive signal from leak-bearing fields, NOT the echoed question: the
        # owner's own content should appear in the answer and/or be cited.
        answer = resp.data.get("answer") or ""
        sources_blob = self._dump(resp.data.get("sources") or [])
        self.assertTrue(
            TOKEN in answer or DOC_ID in sources_blob,
            "Owner cannot see their own content (answer + citations both empty).",
        )


class DuplicateDocIdLeakageTests(DocsearchAuthzTestCase):
    """[RED] Two users with approved KB records under the SAME doc_id must not see
    each other's chunks. A doc_id-only allowlist leaks here: the duplicated doc_id
    is in the requester's own allowlist yet also tags the other user's chunks, so
    visibility must key on per-chunk owner, not doc_id."""

    SHARED_DOC = "shared_name.docx"
    ALICE_SECRET = "alice-only-secret"
    BOB_SECRET = "bob-only-secret"

    def _approve_under_shared_doc(self, user, secret):
        return self._approve_doc(
            user,
            doc_id=self.SHARED_DOC,
            text=f"Confidential laboratory procedure alpha {secret} bravo charlie delta echo.",
        )

    def test_duplicate_doc_id_does_not_leak_other_users_kb_content(self):
        self._approve_under_shared_doc(self.alice, self.ALICE_SECRET)
        self._approve_under_shared_doc(self.bob, self.BOB_SECRET)

        # Bob searches for Alice's token under the shared doc_id -> must not leak.
        self.client.force_authenticate(self.bob)
        resp = self.client.post(
            reverse("doc-search"), {"question": self.ALICE_SECRET, "mode": "docs"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        answer = resp.data.get("answer") or ""
        sources_blob = self._dump(resp.data.get("sources") or [])
        retrieval_blob = self._dump(resp.data.get("retrieval") or {})
        self.assertNotIn(self.ALICE_SECRET, answer)
        self.assertNotIn(self.ALICE_SECRET, sources_blob)
        self.assertNotIn(self.ALICE_SECRET, retrieval_blob)

        # Same invariant through the agent tool.
        result = execute_tool("search_documents", self.bob, {"query": self.ALICE_SECRET})
        self.assertNotIn("error", result, f"tool errored instead of returning a result: {result}")
        for p in result.get("passages", []):
            self.assertNotIn(self.ALICE_SECRET, str(p.get("excerpt", "")))

        # Guard: Bob can still retrieve HIS OWN content under the shared doc_id.
        resp2 = self.client.post(
            reverse("doc-search"), {"question": self.BOB_SECRET, "mode": "docs"}, format="json"
        )
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        own = (resp2.data.get("answer") or "") + self._dump(resp2.data.get("sources") or [])
        self.assertTrue(
            self.BOB_SECRET in own or self.SHARED_DOC in own,
            "Bob cannot retrieve his own content under the shared doc_id.",
        )
