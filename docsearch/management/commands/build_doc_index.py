"""
Build / rebuild the AI Document Search index.

    python manage.py build_doc_index            # reindex the existing corpus
    python manage.py build_doc_index --seed     # copy starter docs from LW-Chatbot, then reindex
    python manage.py build_doc_index --seed --source /path/to/docs

``--seed`` copies supported documents (and standard_responses.xlsx) from the
source directory into the corpus before reindexing. Without it, the command just
re-extracts whatever is already in the corpus directory.
"""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from docsearch import index_store
from docsearch.text_extract import SUPPORTED_EXTS

DEFAULT_SEED_SOURCE = Path("/Users/umair/2026/DSE/LW-Chatbot/Converted")
DEFAULT_FAQ_SOURCE = Path("/Users/umair/2026/DSE/LW-Chatbot/standard_responses.xlsx")


class Command(BaseCommand):
    help = "Build or rebuild the AI Document Search corpus index."

    def add_arguments(self, parser):
        parser.add_argument("--seed", action="store_true",
                            help="Copy starter documents into the corpus before indexing.")
        parser.add_argument("--source", default=str(DEFAULT_SEED_SOURCE),
                            help="Directory of documents to seed from (with --seed).")

    def handle(self, *args, **opts):
        if opts["seed"]:
            src = Path(opts["source"])
            corpus = index_store.corpus_dir()
            copied = 0
            if src.is_dir():
                for p in sorted(src.iterdir()):
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                        shutil.copy2(p, corpus / p.name)
                        copied += 1
                self.stdout.write(self.style.SUCCESS(f"Seeded {copied} document(s) from {src}"))
            else:
                self.stdout.write(self.style.WARNING(f"Seed source not found: {src}"))
            # Copy the FAQ table next to the configured path if available.
            faq_dest = Path(getattr(settings, "DOCSEARCH_FAQ_PATH", ""))
            if faq_dest and DEFAULT_FAQ_SOURCE.exists() and not faq_dest.exists():
                faq_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(DEFAULT_FAQ_SOURCE, faq_dest)
                self.stdout.write(self.style.SUCCESS(f"Seeded FAQ table -> {faq_dest}"))

        self.stdout.write("Rebuilding index…")
        st = index_store.rebuild_index()
        self.stdout.write(self.style.SUCCESS(
            f"Indexed {st['documents']} document(s), {st['chunks']} chunk(s) -> {st['index_path']}"
        ))
