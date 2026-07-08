"""Seed committed demo documents into AI Document Search and rebuild the index.

Ships ready-made reference documents (e.g. ISO/IEC 17025:2017) that live in the
image at ``docsearch/seed_docs/`` straight into the searchable corpus, then
rebuilds the index — all in this management process, so it never hits the
gunicorn request timeout that a synchronous re-embed during an HTTP upload can.

    # production (inside the api container):
    docker compose exec api python manage.py seed_docsearch

    python manage.py seed_docsearch --as-knowledge   # also list in Knowledge Base
    python manage.py seed_docsearch --source /path/to/docs
    python manage.py seed_docsearch --no-reindex      # batch: skip the rebuild

Corpus documents are globally readable (visible to every authenticated user in
Document Search). ``--as-knowledge`` additionally registers each file as an
APPROVED Knowledge Base record (owned by the admin) so it also appears in the
Knowledge Base manager; its chunks dedupe against the global corpus copy.
"""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from docsearch import index_store
from docsearch.text_extract import SUPPORTED_EXTS


class Command(BaseCommand):
    help = ("Seed committed demo documents (e.g. ISO/IEC 17025) into Document "
            "Search and rebuild the index.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", default=None,
            help="Directory of documents to seed (default: docsearch/seed_docs).")
        parser.add_argument(
            "--as-knowledge", action="store_true",
            help="Also register each doc as an APPROVED Knowledge Base record "
                 "(owner = admin) so it shows in the Knowledge Base manager.")
        parser.add_argument(
            "--no-reindex", action="store_true",
            help="Skip the index rebuild (e.g. to seed several sources first).")

    def handle(self, *args, **opts):
        src = Path(opts["source"]) if opts["source"] else (
            Path(settings.BASE_DIR) / "docsearch" / "seed_docs")
        if not src.is_dir():
            self.stderr.write(self.style.ERROR(f"Seed source not found: {src}"))
            return

        corpus = index_store.corpus_dir()
        corpus.mkdir(parents=True, exist_ok=True)
        copied = []
        for p in sorted(src.iterdir()):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                dest = corpus / p.name
                shutil.copy2(p, dest)          # overwrite same-name → idempotent
                copied.append(dest)

        if not copied:
            self.stderr.write(self.style.WARNING(
                f"No supported documents found in {src} "
                f"(supported: {', '.join(sorted(SUPPORTED_EXTS))})."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(copied)} document(s) into the corpus → {corpus}"))
        for d in copied:
            self.stdout.write(f"  • {d.name}")

        # Optional: also register as APPROVED Knowledge Base records.
        if opts["as_knowledge"]:
            self._register_knowledge(copied)

        # Rebuild the searchable index once (BM25 + dense vectors), in-process.
        if opts["no_reindex"]:
            self.stdout.write(self.style.WARNING(
                "Skipped index rebuild (--no-reindex). Run "
                "'manage.py build_doc_index' to make the documents searchable."))
            return

        self.stdout.write("Rebuilding search index (this can take a minute)…")
        st = index_store.rebuild_index()
        self.stdout.write(self.style.SUCCESS(
            f"✓ Indexed {st.get('documents', '?')} document(s), "
            f"{st.get('chunks', '?')} chunk(s)."))
        self.stdout.write(
            "\nAsk in Document Search, e.g.:\n"
            "  • \"What must a calibration certificate include under ISO 17025?\"\n"
            "  • \"What are the requirements for reporting statements of conformity?\"\n"
            "  • \"How does ISO/IEC 17025 define impartiality?\"")

    # ------------------------------------------------------------------------
    def _register_knowledge(self, paths):
        from django.contrib.auth import get_user_model

        from docsearch import ingest
        from docsearch.models import KnowledgeRecord

        User = get_user_model()
        admin = (User.objects.filter(role=getattr(User.Role, "ADMIN", "admin"))
                 .order_by("id").first() or User.objects.order_by("id").first())
        if admin is None:
            self.stderr.write(self.style.WARNING(
                "--as-knowledge skipped: no user to own the records "
                "(run 'manage.py seed_lab' or 'manage.py bootstrap' first)."))
            return

        approved = skipped = superseded = 0
        for dest in paths:
            rec = ingest.ingest_document_file(dest, created_by=admin, source_file=dest.name)
            if rec.status == KnowledgeRecord.Status.DUPLICATE:
                # Identical content already ingested — drop the fresh dup row and
                # leave the existing approved record (and index) untouched.
                rec.delete()
                skipped += 1
                continue
            if rec.validation.get("errors"):
                self.stderr.write(self.style.WARNING(
                    f"  {dest.name}: {rec.validation['errors']} — left pending."))
                skipped += 1
                continue
            # reindex=False: we rebuild once at the end (or the caller does).
            ingest.approve_record(rec, reviewed_by=admin, reindex=False)
            approved += 1
            # New/changed content for this filename: supersede any PRIOR approved
            # or pending record with the same doc_id so re-seeding an EDITED doc
            # doesn't leave stale, frozen passages in the index (content-hash
            # dedupe alone can't catch this — the hash changed).
            stale = (KnowledgeRecord.objects
                     .filter(source_type=KnowledgeRecord.Source.DOCUMENT,
                             doc_id=dest.name, created_by=admin,
                             status__in=[KnowledgeRecord.Status.APPROVED,
                                         KnowledgeRecord.Status.PENDING])
                     .exclude(id=rec.id))
            superseded += stale.count()
            stale.delete()
        msg = (f"Knowledge Base: {approved} approved, {skipped} already-present/"
               f"skipped (owner: {admin.username}).")
        if superseded:
            msg += f" Superseded {superseded} stale record(s) for edited doc(s)."
        self.stdout.write(self.style.SUCCESS(msg))
