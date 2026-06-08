import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import torch
from datasets import load_dataset
from peft import LoraConfig
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    set_seed,
)
from trl import SFTConfig, SFTTrainer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Colab Pro QLoRA SFT training for MentorX")
    parser.add_argument("--train-file", default="train_v2.jsonl")
    parser.add_argument("--val-file", default="val_v2.jsonl")
    parser.add_argument("--model-name", default="Qwen/Qwen2.5-1.5B-Instruct")
    parser.add_argument("--output-dir", default="outputs/mentorx-qwen25-15b-qlora")
    parser.add_argument("--max-length", type=int, default=768)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--train-batch-size", type=int, default=1)
    parser.add_argument("--eval-batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=16)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.03)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--eval-steps", type=int, default=100)
    parser.add_argument("--save-steps", type=int, default=100)
    parser.add_argument("--save-total-limit", type=int, default=2)
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume-from-checkpoint", default=None)
    parser.add_argument("--use-assistant-only-loss", action="store_true")
    parser.add_argument("--packing", action="store_true")
    parser.add_argument("--gradient-checkpointing", action="store_true")
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--push-to-hub", action="store_true")
    parser.add_argument("--hub-model-id", default=None)
    parser.add_argument("--hub-private-repo", action="store_true")
    return parser.parse_args()


def pick_mixed_precision() -> Dict[str, bool]:
    if not torch.cuda.is_available():
        return {"bf16": False, "fp16": False}
    major, _ = torch.cuda.get_device_capability(0)
    # bf16 Ampere (SM80) ve sonrasi kartlarda guvenli.
    if major >= 8:
        return {"bf16": True, "fp16": False}
    return {"bf16": False, "fp16": True}


def validate_messages(rows: List[dict], split_name: str) -> None:
    for i, row in enumerate(rows):
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 3:
            raise RuntimeError(f"{split_name}[{i}] invalid messages")
        if messages[0].get("role") != "system":
            raise RuntimeError(f"{split_name}[{i}] first role system degil")
        if messages[-1].get("role") != "assistant":
            raise RuntimeError(f"{split_name}[{i}] last role assistant degil")
        for mi, msg in enumerate(messages):
            role = msg.get("role")
            content = msg.get("content")
            if role not in {"system", "user", "assistant"}:
                raise RuntimeError(f"{split_name}[{i}] invalid role at {mi}")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError(f"{split_name}[{i}] empty content at {mi}")


def tokenizer_supports_assistant_mask(tokenizer: Any) -> bool:
    probe = [
        {"role": "user", "content": "Merhaba"},
        {"role": "assistant", "content": "Selam"},
    ]
    try:
        out = tokenizer.apply_chat_template(
            probe,
            tokenize=True,
            add_generation_prompt=False,
            return_dict=True,
            return_assistant_tokens_mask=True,
        )
    except Exception:
        return False

    if not isinstance(out, dict):
        return False
    for key, value in out.items():
        lk = str(key).lower()
        if "assistant" in lk and "mask" in lk and isinstance(value, list):
            if any(int(v) == 1 for v in value):
                return True
    return False


def build_sft_config(args: argparse.Namespace, precision: Dict[str, bool]) -> SFTConfig:
    config_values: Dict[str, Any] = {
        "output_dir": args.output_dir,
        "num_train_epochs": args.epochs,
        "per_device_train_batch_size": args.train_batch_size,
        "per_device_eval_batch_size": args.eval_batch_size,
        "gradient_accumulation_steps": args.grad_accum,
        "learning_rate": args.learning_rate,
        "weight_decay": args.weight_decay,
        "warmup_ratio": args.warmup_ratio,
        "logging_steps": args.logging_steps,
        "save_steps": args.save_steps,
        "eval_steps": args.eval_steps,
        "save_total_limit": args.save_total_limit,
        "gradient_checkpointing": args.gradient_checkpointing,
        "packing": args.packing,
        "report_to": "none",
        "remove_unused_columns": False,
        "seed": args.seed,
        "bf16": precision["bf16"],
        "fp16": precision["fp16"],
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "assistant_only_loss": args.use_assistant_only_loss,
        "push_to_hub": args.push_to_hub,
        "hub_model_id": args.hub_model_id,
        "hub_private_repo": args.hub_private_repo,
    }

    fields = set(getattr(SFTConfig, "__dataclass_fields__", {}).keys())

    # TRL/Transformers surum farklari icin alias destegi.
    if "eval_strategy" in fields:
        config_values["eval_strategy"] = "steps"
    elif "evaluation_strategy" in fields:
        config_values["evaluation_strategy"] = "steps"

    if "save_strategy" in fields:
        config_values["save_strategy"] = "steps"

    if "max_length" in fields:
        config_values["max_length"] = args.max_length
    elif "max_seq_length" in fields:
        config_values["max_seq_length"] = args.max_length

    filtered = {k: v for k, v in config_values.items() if k in fields}
    return SFTConfig(**filtered)


def main() -> None:
    args = parse_args()
    set_seed(args.seed)

    train_path = Path(args.train_file)
    val_path = Path(args.val_file)
    if not train_path.exists():
        raise RuntimeError(f"Train dosyasi yok: {train_path}")
    if not val_path.exists():
        raise RuntimeError(f"Val dosyasi yok: {val_path}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    precision = pick_mixed_precision()
    print("=== RUNTIME ===")
    print(f"cuda_available   : {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"gpu_name         : {torch.cuda.get_device_name(0)}")
        print(f"gpu_capability   : {torch.cuda.get_device_capability(0)}")
    print(f"bf16/fp16        : {precision['bf16']}/{precision['fp16']}")

    print("\n=== LOAD TOKENIZER ===")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name, trust_remote_code=args.trust_remote_code)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer.chat_template is None:
        raise RuntimeError("Tokenizer chat_template yok. Instruct/chat model sec.")

    if args.use_assistant_only_loss:
        if not tokenizer_supports_assistant_mask(tokenizer):
            raise RuntimeError(
                "assistant_only_loss aktif ama chat template assistant mask uretmiyor. "
                "Qwen2.5 Instruct ile tekrar dene veya completion_only yaklasimina gec."
            )

    print("\n=== LOAD DATA ===")
    dataset = load_dataset(
        "json",
        data_files={"train": str(train_path), "validation": str(val_path)},
    )

    # Kucuk datasetlerde map overhead'i yerine once ham satirlari dogrula.
    train_rows = [dataset["train"][i] for i in range(len(dataset["train"]))]
    val_rows = [dataset["validation"][i] for i in range(len(dataset["validation"]))]
    validate_messages(train_rows, "train")
    validate_messages(val_rows, "validation")
    print(f"train_rows       : {len(train_rows)}")
    print(f"val_rows         : {len(val_rows)}")

    print("\n=== LOAD MODEL (4-bit QLoRA) ===")
    compute_dtype = torch.bfloat16 if precision["bf16"] else torch.float16
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=args.trust_remote_code,
    )
    model.config.use_cache = False

    peft_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
    )

    sft_config = build_sft_config(args, precision)
    print("\n=== TRAIN CONFIG ===")
    print(json.dumps(sft_config.to_dict(), ensure_ascii=False, indent=2))

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        processing_class=tokenizer,
        peft_config=peft_config,
    )

    print("\n=== TRAIN START ===")
    train_result = trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)

    metrics_path = output_dir / "train_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        json.dump(train_result.metrics, f, ensure_ascii=False, indent=2)

    print("\n=== EVAL ===")
    eval_metrics = trainer.evaluate()
    eval_path = output_dir / "eval_metrics.json"
    with eval_path.open("w", encoding="utf-8") as f:
        json.dump(eval_metrics, f, ensure_ascii=False, indent=2)

    if args.push_to_hub:
        trainer.push_to_hub()

    print("\n=== DONE ===")
    print(f"model_dir        : {args.output_dir}")
    print(f"train_metrics    : {metrics_path}")
    print(f"eval_metrics     : {eval_path}")


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
