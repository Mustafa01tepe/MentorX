import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


KEY_RE = re.compile(r"^_*(\d+)_([A-Za-z0-9]+)$")
VALID_PROFILES = {"FAST", "MEM", "SLOW"}
VALID_ROLES = {"user", "assistant"}


def read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"JSON parse error {path}:{line_no}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: List[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def parse_key(key: str) -> Tuple[str, str]:
    match = KEY_RE.match(key)
    if not match:
        raise RuntimeError(f"Gecersiz _key formati: {key}")
    return match.group(1), match.group(2)


def validate_row(row: dict, idx: int) -> None:
    key = row.get("_key")
    profile = row.get("profile")
    system = row.get("system")
    messages = row.get("messages")

    if not isinstance(key, str) or not key.strip():
        raise RuntimeError(f"Satir {idx}: invalid _key")
    parse_key(key)
    if profile not in VALID_PROFILES:
        raise RuntimeError(f"Satir {idx}: invalid profile={profile}")
    if not isinstance(system, str) or not system.strip():
        raise RuntimeError(f"Satir {idx}: invalid system")
    if not isinstance(messages, list) or len(messages) < 2:
        raise RuntimeError(f"Satir {idx}: invalid messages list")

    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise RuntimeError(f"Satir {idx}: message[{mi}] dict degil")
        role = msg.get("role")
        content = msg.get("content")
        if role not in VALID_ROLES:
            raise RuntimeError(f"Satir {idx}: message[{mi}] invalid role={role}")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Satir {idx}: message[{mi}] empty content")


def extract_group_key(question_id: str, rows: List[dict]) -> str:
    profiles = sorted({str(r.get("profile", "")).strip() for r in rows})
    has_error_key = any(str(r.get("_key", "")).startswith("_") for r in rows)
    coverage = "".join(profiles)
    return f"{coverage}|error={1 if has_error_key else 0}"


def count_profiles(rows: List[dict]) -> Dict[str, int]:
    c = Counter()
    for row in rows:
        c[str(row.get("profile", "")).strip()] += 1
    return dict(sorted(c.items()))


def to_chatml_rows(rows: List[dict]) -> List[dict]:
    out: List[dict] = []
    for row in rows:
        out.append(
            {
                "messages": [{"role": "system", "content": row["system"]}] + row["messages"],
            }
        )
    return out


def validate_chatml_row(row: dict, idx: int) -> None:
    if set(row.keys()) != {"messages"}:
        raise RuntimeError(f"ChatML satir {idx}: top-level alanlar gecersiz")
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 3:
        raise RuntimeError(f"ChatML satir {idx}: invalid messages")
    if messages[0].get("role") != "system":
        raise RuntimeError(f"ChatML satir {idx}: first role system degil")
    if messages[1].get("role") != "user":
        raise RuntimeError(f"ChatML satir {idx}: second role user degil")
    if messages[-1].get("role") != "assistant":
        raise RuntimeError(f"ChatML satir {idx}: last role assistant degil")
    for i, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")
        if role not in {"system", "user", "assistant"}:
            raise RuntimeError(f"ChatML satir {idx}: invalid role at {i}")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"ChatML satir {idx}: empty content at {i}")
        if i == 0:
            continue
        expected = "user" if i % 2 == 1 else "assistant"
        if role != expected:
            raise RuntimeError(f"ChatML satir {idx}: role alternating bozuk at {i}")


def build_splits(
    groups: Dict[str, List[dict]],
    val_ratio: float,
    test_ratio: float,
    seed: int,
) -> Tuple[List[dict], List[dict], List[dict]]:
    rng = random.Random(seed)
    bucketed: Dict[str, List[str]] = defaultdict(list)
    for qid, rows in groups.items():
        bucketed[extract_group_key(qid, rows)].append(qid)

    train_qids: List[str] = []
    val_qids: List[str] = []
    test_qids: List[str] = []

    for bucket, qids in sorted(bucketed.items()):
        qids_copy = list(qids)
        rng.shuffle(qids_copy)
        n = len(qids_copy)
        test_n = int(round(n * test_ratio))
        val_n = int(round(n * val_ratio))
        if test_n + val_n >= n and n >= 3:
            while test_n + val_n >= n:
                if val_n > 0:
                    val_n -= 1
                elif test_n > 0:
                    test_n -= 1
                else:
                    break
        test_part = qids_copy[:test_n]
        val_part = qids_copy[test_n : test_n + val_n]
        train_part = qids_copy[test_n + val_n :]

        test_qids.extend(test_part)
        val_qids.extend(val_part)
        train_qids.extend(train_part)

    def collect(qids: List[str]) -> List[dict]:
        rows: List[dict] = []
        for qid in qids:
            rows.extend(groups[qid])
        return rows

    return collect(train_qids), collect(val_qids), collect(test_qids)


def question_overlap(a: List[dict], b: List[dict]) -> int:
    aq = set()
    bq = set()
    for row in a:
        q, _ = parse_key(str(row["_key"]))
        aq.add(q)
    for row in b:
        q, _ = parse_key(str(row["_key"]))
        bq.add(q)
    return len(aq & bq)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Leakage-free group split by question id")
    parser.add_argument("--input", default="all_passed_with_dialogs.jsonl")
    parser.add_argument("--train-out", default="train_group_raw.jsonl")
    parser.add_argument("--val-out", default="val_group_raw.jsonl")
    parser.add_argument("--test-out", default="test_group_raw.jsonl")
    parser.add_argument("--train-chatml-out", default="train_group_v2.jsonl")
    parser.add_argument("--val-chatml-out", default="val_group_v2.jsonl")
    parser.add_argument("--test-chatml-out", default="test_group_v2.jsonl")
    parser.add_argument("--val-ratio", type=float, default=0.10)
    parser.add_argument("--test-ratio", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.val_ratio < 0 or args.test_ratio < 0 or args.val_ratio + args.test_ratio >= 1.0:
        raise RuntimeError("val_ratio ve test_ratio gecersiz")

    input_path = Path(args.input)
    rows = read_jsonl(input_path)
    if not rows:
        raise RuntimeError(f"Input bos: {input_path}")

    for idx, row in enumerate(rows):
        validate_row(row, idx)

    groups: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        qid, _ = parse_key(str(row["_key"]))
        groups[qid].append(row)

    train_rows, val_rows, test_rows = build_splits(
        groups=groups,
        val_ratio=args.val_ratio,
        test_ratio=args.test_ratio,
        seed=args.seed,
    )

    write_jsonl(Path(args.train_out), train_rows)
    write_jsonl(Path(args.val_out), val_rows)
    write_jsonl(Path(args.test_out), test_rows)

    train_chatml = to_chatml_rows(train_rows)
    val_chatml = to_chatml_rows(val_rows)
    test_chatml = to_chatml_rows(test_rows)

    for idx, row in enumerate(train_chatml):
        validate_chatml_row(row, idx)
    for idx, row in enumerate(val_chatml):
        validate_chatml_row(row, idx)
    for idx, row in enumerate(test_chatml):
        validate_chatml_row(row, idx)

    write_jsonl(Path(args.train_chatml_out), train_chatml)
    write_jsonl(Path(args.val_chatml_out), val_chatml)
    write_jsonl(Path(args.test_chatml_out), test_chatml)

    total = len(rows)
    total_out = len(train_rows) + len(val_rows) + len(test_rows)
    if total != total_out:
        raise RuntimeError(f"Toplam kayit bozuldu: in={total} out={total_out}")

    tv_overlap = question_overlap(train_rows, val_rows)
    tt_overlap = question_overlap(train_rows, test_rows)
    vt_overlap = question_overlap(val_rows, test_rows)
    if tv_overlap or tt_overlap or vt_overlap:
        raise RuntimeError(
            f"Question overlap bulundu: train-val={tv_overlap} train-test={tt_overlap} val-test={vt_overlap}"
        )

    print("=== GROUP SPLIT SUMMARY ===")
    print(f"input_rows       : {len(rows)}")
    print(f"unique_questions : {len(groups)}")
    print(f"train_rows       : {len(train_rows)}")
    print(f"val_rows         : {len(val_rows)}")
    print(f"test_rows        : {len(test_rows)}")
    print(f"train_profiles   : {count_profiles(train_rows)}")
    print(f"val_profiles     : {count_profiles(val_rows)}")
    print(f"test_profiles    : {count_profiles(test_rows)}")
    print(f"q_overlap t-v/t-s/v-s: {tv_overlap}/{tt_overlap}/{vt_overlap}")
    print(f"-> {args.train_out}")
    print(f"-> {args.val_out}")
    print(f"-> {args.test_out}")
    print(f"-> {args.train_chatml_out}")
    print(f"-> {args.val_chatml_out}")
    print(f"-> {args.test_chatml_out}")


if __name__ == "__main__":
    main()
