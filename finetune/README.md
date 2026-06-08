# DIS Fine-Tuning — Domain Adaptation (Phase 6)

Teach a local **Qwen2.5** model the DIS domain — EGPC products/analyses, petroleum
QC, the LIMS data model, and ASTM/ISO standards — with **QLoRA** so the assistant
is more domain-accurate and a smaller (faster/cheaper) model can match the 14B on
in-domain questions. Fully open-source, runs on your own GPU. No data leaves the org.

> **On a Windows + RTX 50-series (Blackwell) workstation, follow
> [SETUP_WINDOWS.md](SETUP_WINDOWS.md)** and use `train_unsloth.py` — Unsloth ships
> Blackwell-ready kernels, trains ~2× faster with ~70% less VRAM, and exports GGUF
> for Ollama directly. The generic `train_qlora.py` (plain transformers+peft+trl)
> below is the fallback for non-Blackwell / Linux/datacenter GPUs.

> The deterministic engines (stats, anomaly, RCA, forecasting) compute the numbers
> regardless of the model — fine-tuning improves the model's *narration and domain
> recall*, not the math.

## ⭐ Recommended first: fine-tune the *retriever*, not the generator

Memorisation-style SFT of the LLM (below) made small models **hallucinate and
loop** on facts they half-learned — a known failure mode. The high-leverage,
low-risk accuracy win is fine-tuning the **embedder** so retrieval surfaces the
right ASTM/ISO + LIMS passage; the base LLM then narrates *grounded* context.
This generalises to any lab by re-mining pairs from its corpus — no model surgery.

```bash
# GPU box, separate env:
pip install -r finetune/requirements-embed.txt
python finetune/train_embedder.py \
    --base-model BAAI/bge-base-en-v1.5 \
    --out finetune/out/bge-lab --epochs 2 --batch-size 32
# optional: add inverse-cloze pairs from the real corpus index
#   --corpus-chunks media/docsearch/<index>.csv
```
It mines query→passage pairs from `data/domain_sft.jsonl` + `glossary.json`
(+ optional corpus), trains with MultipleNegativesRankingLoss, prints
recall@k/MRR/nDCG **before vs after**, and saves a SentenceTransformer folder.
Promote only if recall improves, then:
```
DOCSEARCH_EMBED_MODEL=/abs/path/finetune/out/bge-lab
DOCSEARCH_EMBED_QUERY_PREFIX="Represent this sentence for searching relevant passages:"
python manage.py build_doc_index --rebuild
```

The QLoRA generator track below remains useful for **tone/format/tool-calling**
adaptation — just don't rely on it for fact recall.

## Pipeline

```
prepare_data.py        train_qlora.py            merge + serve         eval_domain.py
(app venv, no GPU)  →   (GPU box, train env)  →   (GPU box)         →   (gate base vs tuned)
EGPC + ASTM + glossary  QLoRA adapter             Ollama GGUF / vLLM    promote if it wins
   → domain_sft.jsonl
```

### 1. Prepare data — app venv, no GPU
```bash
.venv/bin/python finetune/prepare_data.py
```
Builds `finetune/data/domain_sft.jsonl` (chat format) from the seeded **EGPC cached
datasets**, the **ASTM/ISO corpus** (docsearch index), and `glossary.json`. The seed
set is intentionally small — **expand it** before a real run:
- add terms/SOPs to `glossary.json`;
- add more EGPC datasets (per-analysis spec facts, monthly KPIs) and wire them into
  `egpc_examples()`;
- optionally augment with a teacher LLM (have the 14B draft answers from retrieved
  ASTM passages, then review) to reach hundreds–thousands of examples.

### 2. Train — GPU box, SEPARATE env
QLoRA deps conflict with the app's `numpy<2` pin, so use a fresh venv:
```bash
python -m venv .venv-train && source .venv-train/bin/activate
pip install -r finetune/requirements-train.txt
python finetune/train_qlora.py \
    --base-model Qwen/Qwen2.5-7B-Instruct \
    --data finetune/data/domain_sft.jsonl \
    --output finetune/out/qwen2.5-7b-dis-egpc
```
24 GB VRAM comfortably trains the 7B in 4-bit; use `Qwen/Qwen2.5-14B-Instruct` for
maximum quality if VRAM allows. Output is a LoRA adapter.

### 3. Merge + serve
**Ollama (GGUF):** merge the adapter into the base, convert with llama.cpp
(`convert_hf_to_gguf.py` → quantize `q4_k_m`), then `ollama create qwen2.5-dis-egpc
-f Modelfile` (set `PARAMETER num_ctx 16384`, `PARAMETER temperature 0.2`).
**vLLM:** serve the base with `--enable-lora --lora-modules dis=finetune/out/...`,
or merge first and serve the merged model. Either exposes an OpenAI endpoint.

### 4. Eval gating — promote only if it wins
```bash
# domain knowledge (incl. an EGPC-data-specific question)
.venv/bin/python finetune/eval_domain.py --base-url <endpoint> --model <tuned-model>
# standards retrieval/answers
.venv/bin/python manage.py eval_astm --with-answers
```
Compare against the base model's scores. Only deploy the fine-tune if it improves
both — and never regresses the deterministic tool behaviour.

### 5. Deploy into DIS
Point the app at the tuned model (no code change):
```
LLM_PROVIDER=local
LLM_BASE_URL=http://<gpu-host>:8000/v1   # or Ollama 11434/v1
LLM_MODEL=qwen2.5-dis-egpc
```
Run `python manage.py llm_check` and `python manage.py agent_run "…"` to confirm.

## Files
| File | Runs on | Purpose |
|---|---|---|
| `glossary.json` | — | petroleum-QC + EGPC terminology seed |
| `prepare_data.py` | app venv | build the SFT dataset from real data |
| `data/domain_sft.jsonl` | — | generated training set (chat format) |
| `train_embedder.py` | GPU train env | **⭐ fine-tune the retrieval embedder (recommended)** |
| `requirements-embed.txt` | GPU train env | embedder training deps |
| `train_qlora.py` | GPU train env | QLoRA SFT of Qwen2.5 (tone/format/tools) |
| `requirements-train.txt` | GPU train env | QLoRA training deps (separate from app) |
| `eval_domain.py` + `eval_domain.jsonl` | app venv | base-vs-tuned domain gate |
