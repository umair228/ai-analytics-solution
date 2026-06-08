"""
Domain-knowledge eval — compare a base vs fine-tuned model on EGPC/QC/ASTM
questions (Phase 6 gating). Hits an OpenAI-compatible endpoint (Ollama / vLLM),
so it works for both the base model and the merged fine-tuned model once served.

    # base model
    python finetune/eval_domain.py --base-url http://127.0.0.1:11434/v1 --model qwen2.5:7b-instruct
    # fine-tuned (after merge + serve)
    python finetune/eval_domain.py --base-url http://127.0.0.1:8000/v1 --model qwen2.5-dis-egpc

Scores keyword recall on finetune/eval_domain.jsonl. PROMOTE the fine-tune only
if it beats the base here AND on `manage.py eval_astm`. Runs in the app venv
(needs only `openai`).
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key", default="EMPTY")
    ap.add_argument("--data", default=str(HERE / "eval_domain.jsonl"))
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=120)

    items = [json.loads(l) for l in Path(args.data).read_text().splitlines() if l.strip()]
    passed = 0
    kw_hit = 0
    kw_total = 0
    print(f"Evaluating {args.model} on {len(items)} domain questions…\n")
    for it in items:
        kws = [k.lower() for k in it["expect_keywords"]]
        try:
            resp = client.chat.completions.create(
                model=args.model, temperature=0.2, max_tokens=400,
                messages=[{"role": "user", "content": it["question"]}],
            )
            ans = (resp.choices[0].message.content or "").lower()
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {it['id']:26s} ERROR: {exc}")
            kw_total += len(kws)
            continue
        present = [k for k in kws if k in ans]
        kw_hit += len(present)
        kw_total += len(kws)
        ok = len(present) == len(kws)
        passed += int(ok)
        mark = "✓" if ok else "✗"
        miss = "" if ok else f" | missing: {', '.join(k for k in kws if k not in present)}"
        print(f"  {mark} {it['id']:26s} {len(present)}/{len(kws)}{miss}")

    n = len(items)
    print("\n" + "─" * 48)
    print(f"{args.model}: pass {passed}/{n} ({100*passed//n}%)  |  "
          f"keyword recall {kw_hit}/{kw_total} ({100*kw_hit//max(kw_total,1)}%)")


if __name__ == "__main__":
    main()
