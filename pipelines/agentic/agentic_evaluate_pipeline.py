"""
agentic_evaluate_pipeline.py

Sadece değerlendirme pipeline'ı:
- Girdi: <output-dir>/generated.jsonl
- Çıktı: <output-dir>/passed.jsonl, <output-dir>/failed.jsonl
"""

import argparse
import asyncio
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import httpx

from agentic_error_dialog_pipeline import (
    OpenAICompatClient,
    STUDENT_PROMPTS,
    build_profile_quotas,
    ensure_env_loaded,
    load_dedup_state,
    load_done_keys,
    load_jsonl,
    load_profile_counts,
    load_required_api_key,
    process_one_evaluate,
    quotas_completed,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MentorX evaluate-only pipeline")
    parser.add_argument("--output-dir", default="error_dialogs")
    parser.add_argument("--target-count", type=int, default=300)
    parser.add_argument("--profiles", nargs="+", default=["FAST", "MEM", "SLOW"])
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--evaluator-base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--evaluator-model", default="meta-llama/llama-3.3-70b-instruct")
    parser.add_argument("--evaluator-api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=8.0)
    parser.add_argument("--dedup-hamming-threshold", type=int, default=4)
    parser.add_argument(
        "--strict-profile-compliance",
        action="store_true",
        help="Profil dil/üslup uyumu sağlanmazsa PASS kabul etme.",
    )
    parser.add_argument(
        "--skip-failed-retries",
        action="store_true",
        help="failed.jsonl içindeki key'leri tekrar aday yapma.",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    ensure_env_loaded()

    if args.dedup_hamming_threshold < 0 or args.dedup_hamming_threshold > 64:
        raise RuntimeError("--dedup-hamming-threshold 0 ile 64 arasında olmalı")

    evaluator_key = load_required_api_key(args.evaluator_api_key_env)

    profiles = [p for p in args.profiles if p in STUDENT_PROMPTS]
    if not profiles:
        raise RuntimeError("Geçerli profil yok. Kullanılabilir: FAST MEM SLOW")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_file = output_dir / "generated.jsonl"
    passed_file = output_dir / "passed.jsonl"
    failed_file = output_dir / "failed.jsonl"

    if not generated_file.exists():
        raise RuntimeError(f"generated.jsonl bulunamadı: {generated_file}")

    quotas = build_profile_quotas(profiles, args.target_count)
    pass_counts = load_profile_counts(passed_file)
    dedup_exact_by_profile, dedup_simhash_by_profile = load_dedup_state([passed_file], profiles)

    done_paths = [passed_file, failed_file] if args.skip_failed_retries else [passed_file]
    done_keys = load_done_keys(done_paths)

    generated_records = load_jsonl(generated_file)
    candidates: List[Dict[str, Any]] = []
    for rec in generated_records:
        key = str(rec.get("_key", "")).strip()
        profile = str(rec.get("profile", "")).strip()
        if not key or profile not in profiles:
            continue
        if key in done_keys:
            continue
        candidates.append(rec)
    random.Random(args.seed).shuffle(candidates)

    print("=== EVALUATE PIPELINE ===")
    print(f"output_dir      : {output_dir}")
    print(f"generated_file  : {generated_file}")
    print(f"profiles        : {profiles}")
    print(f"quotas          : {quotas}")
    print(f"existing_pass   : {dict(pass_counts)}")
    print(f"existing_done   : {len(done_keys)}")
    print(f"skip_failed_retry: {args.skip_failed_retries}")
    print(f"candidates      : {len(candidates)}")
    print(f"concurrency     : {args.concurrency}")

    if quotas_completed(pass_counts, quotas):
        print("Hedef quota zaten tamamlanmış. Çıkılıyor.")
        return

    evaluator_client = OpenAICompatClient(
        name="evaluator",
        base_url=args.evaluator_base_url,
        api_key=evaluator_key,
        model=args.evaluator_model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        backoff_sec=args.backoff,
    )

    state: Dict[str, Any] = {
        "quotas": quotas,
        "pass_counts": pass_counts,
        "pass_total": 0,
        "fail_total": 0,
        "dedup_exact_by_profile": dedup_exact_by_profile,
        "dedup_simhash_by_profile": dedup_simhash_by_profile,
    }
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    t0 = time.time()

    async with httpx.AsyncClient() as http_client:
        batch_size = max(1, int(args.concurrency))
        cursor = 0
        while cursor < len(candidates):
            if quotas_completed(state["pass_counts"], quotas):
                break

            planned: Counter = Counter()
            batch_items: List[Dict[str, Any]] = []
            while cursor < len(candidates) and len(batch_items) < batch_size:
                rec = candidates[cursor]
                cursor += 1
                profile = str(rec.get("profile", "")).strip()
                remaining = quotas[profile] - state["pass_counts"].get(profile, 0) - planned[profile]
                if remaining <= 0:
                    continue
                batch_items.append(rec)
                planned[profile] += 1

            if not batch_items:
                continue

            tasks = []
            for rec in batch_items:
                tasks.append(
                    process_one_evaluate(
                        rec=rec,
                        state=state,
                        lock=lock,
                        sem=sem,
                        http_client=http_client,
                        evaluator_client=evaluator_client,
                        passed_file=passed_file,
                        failed_file=failed_file,
                        strict_profile_compliance=args.strict_profile_compliance,
                        dedup_hamming_threshold=args.dedup_hamming_threshold,
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    print(f"[WARN] evaluate task exception sızdı: {result}")

    elapsed = time.time() - t0
    print("\n=== EVALUATE SONUÇ ===")
    print(f"pass_new        : {state['pass_total']}")
    print(f"fail_new        : {state['fail_total']}")
    print(f"pass_total_by_p : {dict(state['pass_counts'])}")
    print(f"elapsed_sec     : {elapsed:.1f}")
    print(f"-> {passed_file}")
    print(f"-> {failed_file}")


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
