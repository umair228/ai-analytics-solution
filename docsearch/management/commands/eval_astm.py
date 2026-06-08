"""
Evaluate the DIS AI assistant's retrieval over the ASTM/ISO standards corpus.

    python manage.py eval_astm                 # retrieval-only (fast, deterministic)
    python manage.py eval_astm --with-answers  # also synthesise answers via the LLM
    python manage.py eval_astm --file my_eval.json --top-k 8

Reads an eval set (default docsearch/eval_astm.json) of {question, expect_keywords,
expect_source_contains}. Each item PASSES retrieval if all expected keywords appear
in the retrieved passages; reports retrieval pass-rate, keyword recall, and source
accuracy. With --with-answers it additionally checks the generated answer — the
MOM's "evaluate the AI Assistant's capabilities … retrieval of numerical
specifications, technical requirements and complex contextual information."
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DEFAULT_FILE = Path(settings.BASE_DIR) / "docsearch" / "eval_astm.json"


class Command(BaseCommand):
    help = "Evaluate document retrieval/answers over the ASTM/ISO standards."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE))
        parser.add_argument("--top-k", type=int, default=8)
        parser.add_argument("--with-answers", action="store_true",
                            help="Also synthesise answers via the LLM and score them.")

    def handle(self, *args, **opts):
        path = Path(opts["file"])
        if not path.exists():
            raise CommandError(f"Eval file not found: {path}")
        items = json.loads(path.read_text()).get("items", [])
        if not items:
            raise CommandError("Eval file has no items.")

        from docsearch.index_store import get_index
        index = get_index()
        if not index.ready or index.df.empty:
            raise CommandError("Doc index is empty — run build_doc_index first.")

        top_k = opts["top_k"]
        ret_pass = 0; src_pass = 0; src_total = 0; kw_hit = 0; kw_total = 0
        ans_pass = 0; ans_total = 0
        self.stdout.write(f"Evaluating {len(items)} items (top_k={top_k}, "
                          f"answers={'on' if opts['with_answers'] else 'off'})…\n")

        for it in items:
            q = it["question"]
            rows, meta = index.retrieve(q, top_k=top_k)
            text = " ".join(
                (r.get("chunk_text", "") if hasattr(r, "get") else r["chunk_text"]) or ""
                for r in rows
            ).lower()
            sources = {
                (r.get("doc_id", "") if hasattr(r, "get") else r["doc_id"]) or ""
                for r in rows
            }
            kws = [k.lower() for k in it.get("expect_keywords", [])]
            present = [k for k in kws if k in text]
            kw_hit += len(present); kw_total += len(kws)
            ok = len(present) == len(kws) and bool(kws)
            ret_pass += int(ok)

            src_ok = None
            if it.get("expect_source_contains"):
                src_total += 1
                src_ok = any(it["expect_source_contains"].lower() in s.lower() for s in sources)
                src_pass += int(src_ok)

            mark = "✓" if ok else "✗"
            line = f"  {mark} {it['id']:28s} kw {len(present)}/{len(kws)}"
            if src_ok is not None:
                line += f" | src {'✓' if src_ok else '✗'}"
            if not ok:
                line += f" | missing: {', '.join(k for k in kws if k not in present)}"
            self.stdout.write(line)

            if opts["with_answers"]:
                from docsearch.answer import synthesize_answer
                ans_total += 1
                try:
                    ans = (synthesize_answer(q, rows).get("answer") or "").lower()
                    a_ok = all(k in ans for k in kws) and bool(kws)
                    ans_pass += int(a_ok)
                    self.stdout.write(f"        answer: {'✓' if a_ok else '✗'} "
                                      f"{ans[:90]}…")
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"        answer ERROR: {exc}"))

        n = len(items)
        self.stdout.write("\n" + "─" * 50)
        self.stdout.write(self.style.SUCCESS(
            f"Retrieval pass: {ret_pass}/{n} ({100*ret_pass//n}%)  |  "
            f"keyword recall: {kw_hit}/{kw_total} ({100*kw_hit//max(kw_total,1)}%)"))
        if src_total:
            self.stdout.write(f"Source accuracy: {src_pass}/{src_total} "
                              f"({100*src_pass//src_total}%)")
        if ans_total:
            self.stdout.write(f"Answer pass: {ans_pass}/{ans_total} "
                              f"({100*ans_pass//ans_total}%)")
