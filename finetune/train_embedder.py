"""Fine-tune the DIS retrieval embedder (sentence-transformers / BGE) on
lab-domain query<->passage pairs.

WHY THIS, NOT LLM SFT
---------------------
Fine-tuning the *generator* to memorise facts caused hallucination + infinite
loops (see finetune/README.md). The legitimate accuracy lever is the
*retriever*: teach the embedder the lab's vocabulary (ASTM/ISO, petroleum-QC,
LabWare LIMS) so the correct passage is retrieved, after which the base LLM just
narrates grounded context. It generalises to any lab by re-mining pairs from
that lab's corpus — no risky model surgery.

PAIR SOURCES (use whatever exists)
  1. finetune/data/domain_sft.jsonl   user -> assistant   (question -> answer)
  2. finetune/glossary.json           term -> definition
  3. --corpus-chunks <index.csv>      lead sentence -> rest of chunk (optional;
                                      inverse-cloze self-supervision over the
                                      real corpus)

Trained with MultipleNegativesRankingLoss (in-batch negatives). The QUERY side
receives the same instruction prefix used at inference (docsearch encode_query),
so training and serving match. Output is a SentenceTransformer folder.

USAGE (on the GPU box; pip install -r finetune/requirements-embed.txt)
  python finetune/train_embedder.py \
      --base-model BAAI/bge-base-en-v1.5 \
      --out finetune/out/bge-lab --epochs 2 --batch-size 32

THEN serve it:
  DOCSEARCH_EMBED_MODEL=/abs/path/to/finetune/out/bge-lab
  DOCSEARCH_EMBED_QUERY_PREFIX="Represent this sentence for searching relevant passages:"
  python manage.py build_doc_index --rebuild     # re-embed the corpus
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# BGE v1.5 retrieval instruction (query side only). MiniLM models use "".
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages:"


# --------------------------------------------------------------------------
# Pair mining
# --------------------------------------------------------------------------
def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def pairs_from_sft(path):
    """(user question, assistant answer) from a chat-format JSONL."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs = json.loads(line).get("messages", [])
        except json.JSONDecodeError:
            continue
        q = next((m["content"] for m in msgs if m.get("role") == "user"), None)
        a = next((m["content"] for m in msgs if m.get("role") == "assistant"), None)
        q, a = _clean(q), _clean(a)
        if q and a and len(a) > 15:
            out.append((q, a))
    return out


def pairs_from_glossary(path):
    """(term / "what is <term>?", definition) from glossary.json."""
    out = []
    if not path.exists():
        return out
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("terms", []):
        term, definition = _clean(entry.get("term")), _clean(entry.get("definition"))
        if term and definition:
            out.append((term, definition))
            out.append((f"What is {term}?", definition))
    return out


def pairs_from_corpus(csv_path):
    """Inverse-cloze pairs (lead sentence -> remainder) from a built chunk index
    CSV (the file produced by `manage.py build_doc_index`, column 'chunk_text')."""
    out = []
    p = Path(csv_path)
    if not p.exists():
        print(f"⚠️  --corpus-chunks {p} not found; skipping corpus pairs.")
        return out
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            text = _clean(row.get("chunk_text"))
            if len(text) < 120:
                continue
            sents = re.split(r"(?<=[.!?])\s+", text)
            if len(sents) < 2:
                continue
            query, rest = _clean(sents[0]), _clean(" ".join(sents[1:]))
            if len(query) > 20 and len(rest) > 40:
                out.append((query, rest))
    return out


def dedupe(pairs):
    seen, out = set(), []
    for q, p in pairs:
        key = (q.lower(), p[:80].lower())
        if key not in seen:
            seen.add(key)
            out.append((q, p))
    return out


# --------------------------------------------------------------------------
# Train
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Fine-tune the DIS retrieval embedder.")
    ap.add_argument("--base-model",
                    default=os.environ.get("DOCSEARCH_EMBED_MODEL", "BAAI/bge-base-en-v1.5"))
    ap.add_argument("--out", default=str(HERE / "out" / "bge-lab"))
    ap.add_argument("--sft", default=str(HERE / "data" / "domain_sft.jsonl"))
    ap.add_argument("--glossary", default=str(HERE / "glossary.json"))
    ap.add_argument("--corpus-chunks", default="",
                    help="optional path to a built chunk index CSV (ICT pairs)")
    ap.add_argument("--query-prefix", default=None,
                    help="query instruction prefix; defaults to the BGE prefix, "
                         "pass '' for MiniLM-style models")
    ap.add_argument("--passage-prefix", default="",
                    help="passage prefix (BGE v1.5 uses none)")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--max-pairs", type=int, default=0, help="0 = no cap")
    args = ap.parse_args()

    query_prefix = BGE_QUERY_PREFIX if args.query_prefix is None else args.query_prefix
    if query_prefix and not query_prefix.endswith((" ", ":")):
        query_prefix += " "

    # ---- mine pairs -------------------------------------------------------
    pairs = (pairs_from_sft(Path(args.sft))
             + pairs_from_glossary(Path(args.glossary)))
    if args.corpus_chunks:
        pairs += pairs_from_corpus(args.corpus_chunks)
    pairs = dedupe(pairs)
    if args.max_pairs:
        pairs = pairs[: args.max_pairs]
    if len(pairs) < 10:
        sys.exit(f"Only {len(pairs)} pairs mined — need at least 10. "
                 f"Check {args.sft} / {args.glossary}.")
    print(f"Mined {len(pairs)} query/passage pairs "
          f"(sft+glossary{'+corpus' if args.corpus_chunks else ''}).")

    # heavy imports after arg parsing so --help is instant
    from sentence_transformers import (InputExample, SentenceTransformer,
                                        losses)
    from sentence_transformers.datasets import NoDuplicatesDataLoader
    from sentence_transformers.evaluation import InformationRetrievalEvaluator

    # ---- deterministic train/eval split (every 10th pair held out) --------
    eval_pairs = [p for i, p in enumerate(pairs) if i % 10 == 0]
    train_pairs = [p for i, p in enumerate(pairs) if i % 10 != 0]
    print(f"  train={len(train_pairs)}  eval={len(eval_pairs)}")

    def q(text):
        return query_prefix + text if query_prefix else text

    def d(text):
        return args.passage_prefix + text if args.passage_prefix else text

    train_examples = [InputExample(texts=[q(qq), d(pp)]) for qq, pp in train_pairs]

    model = SentenceTransformer(args.base_model)
    loader = NoDuplicatesDataLoader(train_examples, batch_size=args.batch_size)
    loss = losses.MultipleNegativesRankingLoss(model)

    # ---- IR evaluator (recall@k / MRR / nDCG on the held-out queries) -----
    corpus = {f"d{i}": d(pp) for i, (_, pp) in enumerate(pairs)}      # all passages
    queries = {f"q{i}": q(qq) for i, (qq, _) in enumerate(eval_pairs)}
    # map each held-out query to the global passage id it came from
    rel = {}
    for i, (_, pp) in enumerate(eval_pairs):
        gid = next(j for j, (_, P) in enumerate(pairs) if P == pp)
        rel[f"q{i}"] = {f"d{gid}"}
    evaluator = InformationRetrievalEvaluator(
        queries, corpus, rel, name="lab-retrieval",
        show_progress_bar=False, corpus_chunk_size=2048,
        precision_recall_at_k=[1, 3, 5, 10], mrr_at_k=[10], ndcg_at_k=[10],
    )

    print(f"\nBaseline (before training) — {args.base_model}:")
    try:
        evaluator(model, output_path=None)
    except Exception as exc:  # noqa: BLE001
        print(f"  (baseline eval skipped: {exc})")

    out = str(Path(args.out).resolve())
    warmup = max(10, int(0.1 * len(loader) * args.epochs))
    print(f"\nTraining → {out}  (epochs={args.epochs}, bs={args.batch_size}, "
          f"warmup={warmup})")
    model.fit(
        train_objectives=[(loader, loss)],
        evaluator=evaluator,
        epochs=int(args.epochs) if args.epochs.is_integer() else args.epochs,
        warmup_steps=warmup,
        optimizer_params={"lr": args.lr},
        output_path=out,
        save_best_model=True,
        use_amp=True,
        show_progress_bar=True,
    )

    # persist the prefix so serving uses the matching instruction
    Path(out, "dis_serving.json").write_text(json.dumps({
        "DOCSEARCH_EMBED_MODEL": out,
        "DOCSEARCH_EMBED_QUERY_PREFIX": query_prefix,
        "DOCSEARCH_EMBED_PASSAGE_PREFIX": args.passage_prefix,
    }, indent=2), encoding="utf-8")

    print(f"\n✓ Saved fine-tuned embedder to {out}")
    print("Next: set these in .env and rebuild the index —")
    print(f'  DOCSEARCH_EMBED_MODEL={out}')
    if query_prefix:
        print(f'  DOCSEARCH_EMBED_QUERY_PREFIX="{query_prefix}"')
    print("  python manage.py build_doc_index --rebuild")


if __name__ == "__main__":
    main()
