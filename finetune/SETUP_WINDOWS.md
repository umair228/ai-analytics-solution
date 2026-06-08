# Fine-Tuning DIS on a Windows + NVIDIA RTX 50-series Workstation

End-to-end: set up the GPU box → train the EGPC/QC domain model with **Unsloth** →
export **GGUF** → serve via **Ollama** → point DIS at it. Unsloth is the
recommended path for RTX 50-series (**Blackwell**) because plain PyTorch/bitsandbytes
still need cu128/source builds for `sm_120`, while Unsloth ships Blackwell-ready
kernels and trains ~2× faster with ~70% less VRAM.

> Topology: SQL Server `EGPC_DEV` + the DIS app run on your Mac today. The Windows
> box is the **GPU trainer + model server**. You'll generate the dataset on the Mac,
> copy it over, train on Windows, then either (A) run Ollama on Windows and have DIS
> (on the Mac) point at it over the LAN, or (B) move the DIS backend to Windows too.

---

## 0. Confirm the GPU and pick the model
In PowerShell:
```powershell
nvidia-smi
```
Note the **VRAM**, then choose:

| VRAM | Card examples | Train this |
|---|---|---|
| 12 GB | RTX 5070 | `unsloth/Qwen2.5-7B-Instruct` |
| 16 GB | RTX 5070 Ti / 5080 | 7B (easy) or 14B (fits with Unsloth) |
| 24 GB | RTX 4090 / 3090 | `unsloth/Qwen2.5-14B-Instruct` |
| 32 GB | RTX 5090 | 14B comfortably |

With Unsloth 4-bit QLoRA: 7B ≈ 6–8 GB, 14B ≈ 11–13 GB. **7B is the safe default and is plenty for this domain dataset.**

Also install/confirm: latest **NVIDIA driver**, **Docker Desktop** (with the WSL2 + GPU backend) if using the Docker path.

---

## 1. Get the code + training data onto the Windows box
The dataset is generated on the Mac (where the EGPC DB + DIS live). On the **Mac**:
```bash
cd ai-analytics-solution
.venv/bin/python finetune/prepare_data.py     # → finetune/data/domain_sft.jsonl
```
**Expand it first** for a real run (see finetune/README.md — more glossary terms,
more EGPC datasets, optional teacher augmentation; aim for 300+ examples).

Then copy `finetune/` (at least `data/domain_sft.jsonl`, `train_unsloth.py`,
`eval_domain.py`, `eval_domain.jsonl`) to the Windows box — git, scp, or a shared
folder.

---

## 2. Install Unsloth (pick ONE)

### Option A — Docker (recommended; cleanest for Blackwell)
```powershell
docker run -d -e JUPYTER_PASSWORD="dis" -p 8888:8888 -p 2222:22 `
  -v ${PWD}/work:/workspace/work --gpus all unsloth/unsloth
```
Put the `finetune/` folder under `.\work`, open Jupyter at http://localhost:8888,
or `docker exec -it <container> bash`. The image already contains the correct
CUDA/PyTorch/bitsandbytes/llama.cpp for Blackwell — no version juggling.

### Option B — Native Windows (Conda)
```powershell
conda create --name unsloth_env python==3.12 -y
conda activate unsloth_env
# Blackwell needs cu128+; install the matching CUDA build of PyTorch:
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install unsloth unsloth_zoo bitsandbytes
pip install -U transformers trl datasets
```
(If `xformers` complains on Blackwell, build it with `set TORCH_CUDA_ARCH_LIST=12.0`
then install from source — optional, only speeds things up.)

---

## 3. Train
From the repo root, in the Unsloth env/container:
```bash
# 12 GB (RTX 5070):
python finetune/train_unsloth.py --model unsloth/Qwen2.5-7B-Instruct --out qwen2.5-7b-dis-egpc --gguf q4_k_m
# ≥16 GB:
python finetune/train_unsloth.py --model unsloth/Qwen2.5-14B-Instruct --out qwen2.5-14b-dis-egpc --gguf q4_k_m
```
On this small dataset training takes minutes. Output: a LoRA adapter
(`finetune/out/<name>-lora`) and a GGUF (`finetune/out/<name>-gguf`).

---

## 4. (done by the script) GGUF export
`--gguf q4_k_m` writes a `*.gguf` plus an Ollama `Modelfile`. Use `q5_k_m` or `q8_0`
for higher fidelity if VRAM allows.

---

## 5. Serve with Ollama
Install Ollama for Windows, then create the model from the exported GGUF:
```powershell
cd finetune\out\qwen2.5-7b-dis-egpc-gguf
# Edit the generated Modelfile to add good defaults:
#   PARAMETER num_ctx 16384
#   PARAMETER temperature 0.2
ollama create qwen2.5-7b-dis-egpc -f Modelfile
ollama run qwen2.5-7b-dis-egpc "What is the average sulphur for product STEAM at EGPC?"
```
To let DIS on another machine reach it, start Ollama with `OLLAMA_HOST=0.0.0.0`.

---

## 6. Point DIS at the tuned model
In `ai-analytics-solution/.env` (no code change):
```
LLM_PROVIDER=local
# Ollama on THIS Windows box:           http://127.0.0.1:11434/v1
# DIS on the Mac, Ollama on Windows:    http://<windows-LAN-ip>:11434/v1
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen2.5-7b-dis-egpc
```
Verify:
```bash
.venv/bin/python manage.py llm_check
.venv/bin/python manage.py agent_run "Which product has the worst out-of-spec rate and why?"
```

---

## 7. Eval gate — only promote if it wins
```bash
# domain knowledge (base vs tuned) — run against each model's endpoint
python finetune/eval_domain.py --base-url <endpoint> --model qwen2.5:7b-instruct        # base
python finetune/eval_domain.py --base-url <endpoint> --model qwen2.5-7b-dis-egpc         # tuned
# standards retrieval/answers
python manage.py eval_astm --with-answers
```
Keep the fine-tune only if it beats the base on `eval_domain` (esp. the EGPC-specific
`steam_offspec` item) and doesn't regress `eval_astm`.

---

## 8. Iterate
Expand `domain_sft.jsonl` (more EGPC facts, SOP/method Q&A, teacher-augmented ASTM
answers) → retrain → re-eval. The deterministic analytics never change; you're only
improving the model's domain narration.

## Sources
- [Unsloth — Windows install](https://unsloth.ai/docs/get-started/install/windows-installation)
- [Unsloth — fine-tuning on Blackwell / RTX 50-series](https://unsloth.ai/docs/blog/fine-tuning-llms-with-blackwell-rtx-50-series-and-unsloth)
- [NVIDIA — Fine-tune LLMs on RTX GPUs with Unsloth](https://developer.nvidia.com/blog/train-an-llm-on-an-nvidia-blackwell-desktop-with-unsloth-and-scale-it/)
