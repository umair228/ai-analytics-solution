"""
QLoRA fine-tune of Qwen2.5 with **Unsloth** — recommended for the EGPC workstation
(NVIDIA RTX 50-series / Blackwell, Windows). Unsloth ships Blackwell-ready CUDA
kernels (sidestepping the sm_120 PyTorch problem), trains ~2x faster with ~70%
less VRAM, and exports straight to **GGUF for Ollama** — so the tuned model drops
into DIS with just an LLM_MODEL change.

Run inside the Unsloth Docker image or an Unsloth env (see finetune/SETUP_WINDOWS.md):

    python finetune/train_unsloth.py \
        --model unsloth/Qwen2.5-7B-Instruct \
        --data finetune/data/domain_sft.jsonl \
        --out  qwen2.5-7b-dis-egpc \
        --gguf q4_k_m

VRAM guide (Unsloth 4-bit QLoRA): 7B ≈ 6-8 GB, 14B ≈ 11-13 GB. Use 7B on a 12 GB
RTX 5070; step up to --model unsloth/Qwen2.5-14B-Instruct if you have ≥16 GB.
"""
import argparse
import json
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Unsloth QLoRA SFT for Qwen2.5 (DIS/EGPC).")
    p.add_argument("--model", default="unsloth/Qwen2.5-7B-Instruct",
                   help="Unsloth 4-bit model id (…14B-Instruct for ≥16GB VRAM).")
    p.add_argument("--data", default="finetune/data/domain_sft.jsonl")
    p.add_argument("--out", default="qwen2.5-7b-dis-egpc", help="Output dir / model name.")
    p.add_argument("--epochs", type=float, default=2.0)  # 1–2 avoids overfit on small sets
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--max-seq-len", type=int, default=2048)
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--gguf", default="q4_k_m",
                   help="GGUF quant for Ollama export (q4_k_m / q5_k_m / q8_0); "
                        "'none' to skip and keep the LoRA adapter only.")
    return p.parse_args()


def main():
    args = parse_args()
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model, max_seq_length=args.max_seq_len,
        load_in_4bit=True, dtype=None,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.0,
        bias="none", use_gradient_checkpointing="unsloth", random_state=42,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )

    tokenizer = get_chat_template(tokenizer, chat_template="qwen-2.5")

    def fmt(ex):
        return {"text": tokenizer.apply_chat_template(
            ex["messages"], tokenize=False, add_generation_prompt=False)}

    n = sum(1 for _ in Path(args.data).open())
    ds = load_dataset("json", data_files=args.data, split="train").map(fmt)
    print(f"Loaded {n} examples from {args.data}")

    # Resolve to an ABSOLUTE path up front: installing llama.cpp (GGUF step)
    # changes the working directory, which would break a relative output path.
    out_dir = str(Path(f"finetune/out/{args.out}").resolve())

    trainer = SFTTrainer(
        model=model, tokenizer=tokenizer, train_dataset=ds,
        args=SFTConfig(
            output_dir=out_dir,
            dataset_text_field="text", max_seq_length=args.max_seq_len,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs, learning_rate=args.lr,
            warmup_ratio=0.03, lr_scheduler_type="cosine",
            logging_steps=5, optim="adamw_8bit", seed=42, report_to="none",
        ),
    )
    trainer.train()

    adapter_dir = out_dir + "-lora"
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)
    print(f"✓ LoRA adapter -> {adapter_dir}")

    if args.gguf and args.gguf.lower() != "none":
        # Writes a GGUF + an Ollama Modelfile; needs llama.cpp (bundled in the
        # Unsloth Docker image, else Unsloth fetches/builds it). Absolute path
        # (out_dir) is required — the llama.cpp install cd's away from cwd.
        gguf_dir = out_dir + "-gguf"
        model.save_pretrained_gguf(gguf_dir, tokenizer, quantization_method=args.gguf)
        print(f"✓ GGUF ({args.gguf}) -> {gguf_dir}")
        print("Next: ollama create — see finetune/SETUP_WINDOWS.md step 5.")


if __name__ == "__main__":
    main()
