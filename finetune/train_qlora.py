"""
QLoRA supervised fine-tune of Qwen2.5 for DIS domain adaptation (Phase 6).

RUNS ON THE GPU BOX (BusinessWare), in a SEPARATE training environment — NOT the
app venv (these deps pull a CUDA torch / numpy>=2 that would conflict with the
NeuralProphet numpy<2 pin). See finetune/README.md.

    pip install -r finetune/requirements-train.txt
    python finetune/train_qlora.py \
        --base-model Qwen/Qwen2.5-7B-Instruct \
        --data finetune/data/domain_sft.jsonl \
        --output finetune/out/qwen2.5-7b-dis-egpc

Produces a LoRA adapter; merge + convert (GGUF for Ollama, or serve the adapter
with vLLM) per the README, then point DIS LLM_MODEL/LLM_BASE_URL at it.
"""
import argparse
import json
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="QLoRA SFT for Qwen2.5 (DIS domain).")
    p.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct",
                   help="HF model id (use the 14B for max quality if VRAM allows).")
    p.add_argument("--data", default="finetune/data/domain_sft.jsonl")
    p.add_argument("--output", default="finetune/out/qwen2.5-dis-egpc")
    p.add_argument("--epochs", type=float, default=3.0)
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--max-seq-len", type=int, default=2048)
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    return p.parse_args()


def main():
    args = parse_args()
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              BitsAndBytesConfig)
    from trl import SFTConfig, SFTTrainer

    tok = AutoTokenizer.from_pretrained(args.base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    # Preformat each chat sample to a single string via Qwen's chat template.
    rows = [json.loads(l) for l in Path(args.data).read_text().splitlines() if l.strip()]
    texts = [tok.apply_chat_template(r["messages"], tokenize=False,
                                     add_generation_prompt=False) for r in rows]
    ds = Dataset.from_dict({"text": texts})
    print(f"Loaded {len(ds)} training examples from {args.data}")

    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model, quantization_config=bnb, device_map="auto",
        torch_dtype=torch.bfloat16,
    )
    model.config.use_cache = False

    peft_cfg = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.05,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    cfg = SFTConfig(
        output_dir=args.output,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        max_seq_length=args.max_seq_len,
        logging_steps=10,
        save_strategy="epoch",
        bf16=True,
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        dataset_text_field="text",
        report_to="none",
    )
    trainer = SFTTrainer(model=model, train_dataset=ds, args=cfg,
                         peft_config=peft_cfg, processing_class=tok)
    trainer.train()
    trainer.save_model(args.output)
    tok.save_pretrained(args.output)
    print(f"\n✓ LoRA adapter saved -> {args.output}")
    print("Next: merge + convert (see finetune/README.md), then eval_domain.py.")


if __name__ == "__main__":
    main()
