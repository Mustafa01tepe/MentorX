import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


INPUT_FILES = [
    Path("Maverick/mv_pass.jsonl"),
    Path("Maverick2/mv_pass.jsonl"),
    Path("error_dialogs_run1/passed.jsonl"),
]

FILE_PRIORITY = {str(path): i for i, path in enumerate(INPUT_FILES)}
KEY_RE = re.compile(r"^(\d+)_([A-Za-z0-9]+)$")
WS_RE = re.compile(r"\s+")
VALID_PROFILES = {"FAST", "MEM", "SLOW"}


@dataclass
class RecordRef:
    obj: Dict[str, Any]
    src_path: str
    src_idx: int
    line_no: int
    msg_hash: Optional[str]


def normalize_ws(text: Any) -> str:
    return WS_RE.sub(" ", str("" if text is None else text)).strip()


def messages_hash(messages: Any) -> Optional[str]:
    if not isinstance(messages, list):
        return None
    parts = []
    for msg in messages:
        if isinstance(msg, dict):
            role = normalize_ws(msg.get("role"))
            content = normalize_ws(msg.get("content"))
        else:
            role = ""
            content = normalize_ws(msg)
        parts.append(f"{role}\n{content}")
    joined = "\n---\n".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def parse_key(key: Any) -> Tuple[Optional[str], Optional[str]]:
    m = KEY_RE.match(str(key))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def source_rank(rec: RecordRef) -> Tuple[int, int]:
    return rec.src_idx, rec.line_no


def conflict_quality_key(rec: RecordRef) -> Tuple[int, int, int, int, int]:
    judge = rec.obj.get("judge")
    judge_verdict = judge.get("verdict") if isinstance(judge, dict) else None
    judge_pass = 1 if judge_verdict == "pass" else 0

    qc_flags = rec.obj.get("qc_flags")
    retry_count = qc_flags.get("tutor_retry_count") if isinstance(qc_flags, dict) else None
    if not isinstance(retry_count, int):
        retry_count = 10**9

    msgs = rec.obj.get("messages")
    msg_len = len(msgs) if isinstance(msgs, list) else 0
    in_target_len = 1 if 8 <= msg_len <= 16 else 0
    len_penalty = 0 if in_target_len else min(abs(msg_len - 8), abs(msg_len - 16))

    # Sort ascending by this tuple after negating pass/target preferences.
    return (-judge_pass, retry_count, -in_target_len, len_penalty, rec.src_idx)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def run_cleanup() -> Dict[str, Any]:
    loaded: List[RecordRef] = []
    per_file_counts: Dict[str, int] = {}

    for i, in_path in enumerate(INPUT_FILES):
        count = 0
        with in_path.open("r", encoding="utf-8") as f:
            for ln, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                count += 1
                loaded.append(
                    RecordRef(
                        obj=obj,
                        src_path=str(in_path),
                        src_idx=i,
                        line_no=ln,
                        msg_hash=messages_hash(obj.get("messages")),
                    )
                )
        per_file_counts[str(in_path)] = count

    total_raw = len(loaded)
    key_counts = Counter(r.obj.get("_key") for r in loaded)
    unique_key_count = len(key_counts)

    profile_counts = Counter()
    question_set = set()
    for rec in loaded:
        q, p = parse_key(rec.obj.get("_key"))
        if q is not None and p is not None:
            question_set.add(q)
            profile_counts[p] += 1

    by_key: Dict[Any, List[RecordRef]] = defaultdict(list)
    for rec in loaded:
        by_key[rec.obj.get("_key")].append(rec)

    clean_records: List[RecordRef] = []
    duplicate_kept: List[RecordRef] = []
    conflict_selected: List[RecordRef] = []
    conflict_rejected_rows: List[Dict[str, Any]] = []

    duplicate_group_count = 0
    duplicate_group_record_count = 0
    conflict_group_count = 0
    conflict_group_record_count = 0

    for key, group in by_key.items():
        if len(group) == 1:
            clean_records.append(group[0])
            continue

        hashes = [g.msg_hash for g in group]
        all_have_messages = all(h is not None for h in hashes)

        if all_have_messages and len(set(hashes)) == 1:
            duplicate_group_count += 1
            duplicate_group_record_count += len(group)
            kept = sorted(group, key=source_rank)[0]
            duplicate_kept.append(kept)
            continue

        conflict_group_count += 1
        conflict_group_record_count += len(group)
        ranked = sorted(group, key=conflict_quality_key)
        winner = ranked[0]
        losers = ranked[1:]
        conflict_selected.append(winner)
        for loser in losers:
            out = dict(loser.obj)
            out["_reject_meta"] = {
                "reason": "key_conflict",
                "key": key,
                "winner_source": winner.src_path,
                "loser_source": loser.src_path,
                "loser_line": loser.line_no,
            }
            conflict_rejected_rows.append(out)

    pre_content_records: List[RecordRef] = clean_records + duplicate_kept + conflict_selected

    # Content duplicate pass (only among records with messages).
    no_messages_records = [r for r in pre_content_records if r.msg_hash is None]
    with_messages_records = [r for r in pre_content_records if r.msg_hash is not None]

    by_content_hash: Dict[str, List[RecordRef]] = defaultdict(list)
    for rec in with_messages_records:
        by_content_hash[rec.msg_hash].append(rec)

    kept_after_content: List[RecordRef] = []
    content_dup_rows: List[Dict[str, Any]] = []
    same_question_diff_profile_count = 0
    real_content_dup_rejected_count = 0
    real_content_dup_kept_count = 0
    real_content_dup_group_count = 0

    for h, group in by_content_hash.items():
        if len(group) == 1:
            kept_after_content.append(group[0])
            continue

        parsed = []
        for rec in group:
            q, p = parse_key(rec.obj.get("_key"))
            parsed.append((q, p, rec))

        qset = {q for q, _, _ in parsed}
        if len(qset) == 1 and None not in qset:
            same_question_diff_profile_count += len(group)
            kept_after_content.extend(group)
            continue

        real_content_dup_group_count += 1
        kept = sorted(group, key=source_rank)[0]
        losers = [r for r in group if r is not kept]
        kept_after_content.append(kept)
        real_content_dup_kept_count += 1
        real_content_dup_rejected_count += len(losers)

        for loser in losers:
            out = dict(loser.obj)
            out["_reject_meta"] = {
                "reason": "content_duplicate",
                "hash": h,
                "winner_source": kept.src_path,
                "loser_source": loser.src_path,
                "loser_line": loser.line_no,
            }
            content_dup_rows.append(out)

    final_records = no_messages_records + kept_after_content
    final_rows = [r.obj for r in final_records]

    all_clean_path = Path("all_passed_clean.jsonl")
    conflict_rejected_path = Path("conflict_rejected.jsonl")
    content_dup_path = Path("content_dup.jsonl")
    write_jsonl(all_clean_path, final_rows)
    write_jsonl(conflict_rejected_path, conflict_rejected_rows)
    write_jsonl(content_dup_path, content_dup_rows)

    final_profile_counts = Counter()
    final_questions = Counter()
    for row in final_rows:
        q, p = parse_key(row.get("_key"))
        if q is not None and p is not None:
            final_profile_counts[p] += 1
            final_questions[q] += 1

    avg_dialog_per_question = (
        round(sum(final_questions.values()) / len(final_questions), 4) if final_questions else 0.0
    )

    return {
        "per_file_counts": per_file_counts,
        "total_raw": total_raw,
        "unique_key_count": unique_key_count,
        "profile_counts": dict(sorted(profile_counts.items())),
        "unique_question_count": len(question_set),
        "clean_count": len(clean_records),
        "duplicate_group_count": duplicate_group_count,
        "duplicate_group_record_count": duplicate_group_record_count,
        "conflict_group_count": conflict_group_count,
        "conflict_group_record_count": conflict_group_record_count,
        "conflict_selected_count": len(conflict_selected),
        "content_dup_group_count": real_content_dup_group_count,
        "content_dup_rejected_count": real_content_dup_rejected_count,
        "content_dup_kept_count": real_content_dup_kept_count,
        "same_question_diff_profile_count": same_question_diff_profile_count,
        "final_total": len(final_rows),
        "final_profile_counts": dict(sorted(final_profile_counts.items())),
        "avg_dialog_per_question": avg_dialog_per_question,
        "paths": {
            "all_clean": str(all_clean_path),
            "conflict_rejected": str(conflict_rejected_path),
            "content_dup": str(content_dup_path),
        },
    }


def run_chatml_conversion() -> Dict[str, Any]:
    in_path = Path("all_passed_clean.jsonl")
    invalid_rows = []
    valid_for_transform = []
    total_loaded = 0

    with in_path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            total_loaded += 1
            row = json.loads(line)
            reasons = []

            key = row.get("_key")
            if not isinstance(key, str) or not key.strip():
                reasons.append("invalid__key")

            profile = row.get("profile")
            if profile not in VALID_PROFILES:
                reasons.append("invalid_profile")

            system = row.get("system")
            if not isinstance(system, str) or not system.strip():
                reasons.append("invalid_system")

            messages = row.get("messages")
            if not isinstance(messages, list) or len(messages) < 2:
                reasons.append("invalid_messages_list")
            else:
                for i, msg in enumerate(messages):
                    if not isinstance(msg, dict):
                        reasons.append(f"message_not_object_{i}")
                        continue
                    if "role" not in msg:
                        reasons.append(f"missing_role_{i}")
                    if "content" not in msg:
                        reasons.append(f"missing_content_{i}")
                    role = msg.get("role")
                    if role not in {"user", "assistant"}:
                        reasons.append(f"invalid_role_{i}")

            if reasons:
                bad = dict(row)
                bad["_invalid_meta"] = {"line": ln, "reasons": sorted(set(reasons))}
                invalid_rows.append(bad)
                continue

            chatml = {
                "messages": [{"role": "system", "content": row["system"]}] + row["messages"],
            }
            valid_for_transform.append(
                {
                    "_key": row["_key"],
                    "profile": row["profile"],
                    "chatml": chatml,
                }
            )

    malformed_rows = []
    qc_pass = []
    malformed_reason_counts = Counter()

    for item in valid_for_transform:
        messages = item["chatml"]["messages"]
        reasons = []

        if not messages or messages[0].get("role") != "system":
            reasons.append("no_system_prefix")

        if len(messages) < 2 or messages[1].get("role") != "user":
            reasons.append("first_after_system_not_user")

        for idx, msg in enumerate(messages):
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                reasons.append("empty_content")
                break
            if idx == 0:
                continue
            expected_role = "user" if idx % 2 == 1 else "assistant"
            if msg.get("role") != expected_role:
                reasons.append("role_not_alternating")
                break

        if messages and messages[-1].get("role") != "assistant":
            reasons.append("last_not_assistant")

        if reasons:
            malformed_reason_counts.update(set(reasons))
            malformed_rows.append(
                {
                    "_key": item["_key"],
                    "profile": item["profile"],
                    "chatml": item["chatml"],
                    "_malformed_meta": {"reasons": sorted(set(reasons))},
                }
            )
            continue

        qc_pass.append(item)

    rnd = random.Random(42)
    grouped = defaultdict(list)
    for item in qc_pass:
        grouped[item["profile"]].append(item)

    train_items = []
    val_items = []
    split_counts = {"train": Counter(), "val": Counter()}

    for profile in sorted(VALID_PROFILES):
        group = grouped.get(profile, [])
        rnd.shuffle(group)
        n = len(group)
        if n == 0:
            continue
        val_n = int(round(n * 0.10))
        if val_n == 0:
            val_n = 1
        if val_n >= n and n > 1:
            val_n = n - 1
        val_group = group[:val_n]
        train_group = group[val_n:]
        val_items.extend(val_group)
        train_items.extend(train_group)
        split_counts["train"][profile] += len(train_group)
        split_counts["val"][profile] += len(val_group)

    rnd.shuffle(train_items)
    rnd.shuffle(val_items)

    train_rows = [item["chatml"] for item in train_items]
    val_rows = [item["chatml"] for item in val_items]
    invalid_out = Path("chatml_invalid.jsonl")
    malformed_out = Path("chatml_malformed.jsonl")
    train_out = Path("train.jsonl")
    val_out = Path("val.jsonl")

    write_jsonl(invalid_out, invalid_rows)
    write_jsonl(malformed_out, malformed_rows)
    write_jsonl(train_out, train_rows)
    write_jsonl(val_out, val_rows)

    def validate_out(path: Path) -> Tuple[int, bool]:
        total = 0
        ok = True
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                total += 1
                try:
                    row = json.loads(line)
                except Exception:
                    ok = False
                    continue
                msgs = row.get("messages")
                if not isinstance(msgs, list) or not msgs:
                    ok = False
                    continue
                if msgs[0].get("role") != "system":
                    ok = False
                if msgs[-1].get("role") != "assistant":
                    ok = False
        return total, ok

    train_lines, train_ok = validate_out(train_out)
    val_lines, val_ok = validate_out(val_out)

    turkish_chars = "çğıöşüÇĞİÖŞÜ"
    turkish_ok = False
    turkish_example = ""
    for path in (train_out, val_out):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for m in row.get("messages", []):
                    content = m.get("content")
                    if isinstance(content, str) and any(ch in content for ch in turkish_chars):
                        turkish_ok = True
                        turkish_example = content[:120]
                        break
                if turkish_ok:
                    break
        if turkish_ok:
            break

    return {
        "total_loaded": total_loaded,
        "valid_count": len(valid_for_transform),
        "invalid_count": len(invalid_rows),
        "qc_pass_count": len(qc_pass),
        "qc_fail_count": len(malformed_rows),
        "qc_fail_reasons": dict(sorted(malformed_reason_counts.items())),
        "train_count": len(train_rows),
        "val_count": len(val_rows),
        "train_profile_counts": dict(sorted(split_counts["train"].items())),
        "val_profile_counts": dict(sorted(split_counts["val"].items())),
        "train_lines": train_lines,
        "train_ok": train_ok,
        "val_lines": val_lines,
        "val_ok": val_ok,
        "turkish_ok": turkish_ok,
        "turkish_example": turkish_example,
        "paths": {
            "invalid": str(invalid_out),
            "malformed": str(malformed_out),
            "train": str(train_out),
            "val": str(val_out),
        },
    }


def main() -> None:
    cleanup = run_cleanup()
    chatml = run_chatml_conversion()
    summary = {"cleanup": cleanup, "chatml": chatml}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
