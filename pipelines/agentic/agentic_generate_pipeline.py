"""
agentic_generate_pipeline.py

Sadece üretim pipeline'ı:
- 2 LLM kullanır: student + tutor
- Çıktı: <output-dir>/generated.jsonl
- Hata kayıtları: <output-dir>/generate_failed.jsonl
"""

import argparse
import asyncio
import json
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx

from agentic_error_dialog_pipeline import (
    OpenAICompatClient,
    STUDENT_PROMPTS,
    build_profile_quotas,
    ensure_distinct_llms,
    ensure_env_loaded,
    load_done_keys,
    load_profile_counts,
    load_required_api_key,
    process_one_generate,
    quotas_completed,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MentorX generate-only pipeline (2 LLM)")
    parser.add_argument("--question-bank", default="Other Versions/question_bank.json")
    parser.add_argument("--output-dir", default="error_dialogs")
    parser.add_argument("--target-count", type=int, default=300)
    parser.add_argument("--profiles", nargs="+", default=["FAST", "MEM", "SLOW"])
    parser.add_argument("--min-turns", type=int, default=4)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--start-qi", type=int, default=0)
    parser.add_argument("--max-questions", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--student-base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--tutor-base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--student-model", default="deepseek/deepseek-chat-v3-0324")
    parser.add_argument("--tutor-model", default="meta-llama/llama-4-maverick")
    parser.add_argument("--student-api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--tutor-api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=8.0)
    parser.add_argument("--reply-min-chars", type=int, default=24)
    parser.add_argument("--reply-max-retries", type=int, default=2)
    parser.add_argument("--history-max-messages", type=int, default=24)
    parser.add_argument(
        "--skip-failed-retries",
        action="store_true",
        help="generate_failed.jsonl içindeki key'leri tekrar aday yapma.",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    ensure_env_loaded()

    if args.min_turns < 1 or args.max_turns < 1:
        raise RuntimeError("--min-turns ve --max-turns en az 1 olmalı")
    if args.min_turns > args.max_turns:
        raise RuntimeError("--min-turns, --max-turns değerinden büyük olamaz")
    if args.reply_min_chars < 1:
        raise RuntimeError("--reply-min-chars en az 1 olmalı")
    if args.reply_max_retries < 0:
        raise RuntimeError("--reply-max-retries 0 veya daha büyük olmalı")
    if args.history_max_messages < 2:
        raise RuntimeError("--history-max-messages en az 2 olmalı")

    student_key = load_required_api_key(args.student_api_key_env)
    tutor_key = load_required_api_key(args.tutor_api_key_env)
    ensure_distinct_llms(
        student_base_url=args.student_base_url,
        student_model=args.student_model,
        tutor_base_url=args.tutor_base_url,
        tutor_model=args.tutor_model,
    )

    profiles = [p for p in args.profiles if p in STUDENT_PROMPTS]
    if not profiles:
        raise RuntimeError("Geçerli profil yok. Kullanılabilir: FAST MEM SLOW")

    qb_path = Path(args.question_bank)
    if not qb_path.exists():
        raise RuntimeError(f"Question bank bulunamadı: {qb_path}")
    with qb_path.open(encoding="utf-8") as f:
        questions = json.load(f)
    if not isinstance(questions, list):
        raise RuntimeError("Question bank list formatında olmalı")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_file = output_dir / "generated.jsonl"
    failed_file = output_dir / "generate_failed.jsonl"

    quotas = build_profile_quotas(profiles, args.target_count)
    generated_counts = load_profile_counts(generated_file)

    done_paths = [generated_file, failed_file] if args.skip_failed_retries else [generated_file]
    done_keys = load_done_keys(done_paths)

    start_qi = max(0, int(args.start_qi))
    selected_questions = questions[start_qi:]
    if args.max_questions and args.max_questions > 0:
        selected_questions = selected_questions[: args.max_questions]

    candidates: List[Tuple[int, str, Dict[str, Any]]] = []
    for local_idx, q in enumerate(selected_questions):
        qi = start_qi + local_idx
        for profile in profiles:
            key = f"{qi}_{profile}"
            if key not in done_keys:
                candidates.append((qi, profile, q))
    random.Random(args.seed).shuffle(candidates)

    print("=== GENERATE PIPELINE (2 LLM) ===")
    print(f"question_bank    : {qb_path}")
    print(f"output_dir       : {output_dir}")
    print(f"generated_file   : {generated_file}")
    print(f"generate_failed  : {failed_file}")
    print(f"profiles         : {profiles}")
    print(f"quotas           : {quotas}")
    print(f"existing_gen_by_p: {dict(generated_counts)}")
    print(f"existing_done    : {len(done_keys)}")
    print(f"skip_failed_retry: {args.skip_failed_retries}")
    print(f"candidates       : {len(candidates)}")
    print(f"concurrency      : {args.concurrency}")

    if quotas_completed(generated_counts, quotas):
        print("Hedef quota zaten üretilmiş. Çıkılıyor.")
        return

    student_client = OpenAICompatClient(
        name="student",
        base_url=args.student_base_url,
        api_key=student_key,
        model=args.student_model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        backoff_sec=args.backoff,
    )
    tutor_client = OpenAICompatClient(
        name="tutor",
        base_url=args.tutor_base_url,
        api_key=tutor_key,
        model=args.tutor_model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        backoff_sec=args.backoff,
    )

    state: Dict[str, Any] = {
        "quotas": quotas,
        "generated_counts": generated_counts,
        "gen_total": 0,
        "skip_total": 0,
        "fail_total": 0,
    }
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    t0 = time.time()

    async with httpx.AsyncClient() as http_client:
        batch_size = max(1, int(args.concurrency))
        cursor = 0
        while cursor < len(candidates):
            if quotas_completed(state["generated_counts"], quotas):
                break

            planned: Counter = Counter()
            batch_items: List[Tuple[int, str, Dict[str, Any]]] = []
            while cursor < len(candidates) and len(batch_items) < batch_size:
                item = candidates[cursor]
                cursor += 1
                _, profile, _ = item
                remaining = quotas[profile] - state["generated_counts"].get(profile, 0) - planned[profile]
                if remaining <= 0:
                    continue
                batch_items.append(item)
                planned[profile] += 1

            if not batch_items:
                continue

            tasks = []
            for item in batch_items:
                tasks.append(
                    process_one_generate(
                        item=item,
                        state=state,
                        lock=lock,
                        sem=sem,
                        http_client=http_client,
                        student_client=student_client,
                        tutor_client=tutor_client,
                        generated_file=generated_file,
                        failed_file=failed_file,
                        min_turns=args.min_turns,
                        max_turns=args.max_turns,
                        reply_min_chars=args.reply_min_chars,
                        reply_max_retries=args.reply_max_retries,
                        history_max_messages=args.history_max_messages,
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    print(f"[WARN] generate task exception sızdı: {result}")

    elapsed = time.time() - t0
    print("\n=== GENERATE SONUÇ ===")
    print(f"generated_new   : {state['gen_total']}")
    print(f"skipped_quota   : {state['skip_total']}")
    print(f"fail_new        : {state['fail_total']}")
    print(f"generated_by_p  : {dict(state['generated_counts'])}")
    print(f"elapsed_sec     : {elapsed:.1f}")
    print(f"-> {generated_file}")
    print(f"-> {failed_file}")


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
