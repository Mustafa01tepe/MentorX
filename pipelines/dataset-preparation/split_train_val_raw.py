import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


INPUT_PATH = Path("all_passed_with_dialogs.jsonl")
RUN1_PASSED_PATH = Path("error_dialogs_run1/passed.jsonl")
M1_PASS_PATH = Path("Maverick/mv_pass.jsonl")
M2_PASS_PATH = Path("Maverick2/mv_pass.jsonl")

TRAIN_OUT = Path("train_raw.jsonl")
VAL_OUT = Path("val_raw.jsonl")

VALID_PROFILES = {"FAST", "MEM", "SLOW"}
VALID_ROLES = {"user", "assistant"}
SEED = 42
VAL_RATIO = 0.10
EXPECTED_TOTAL = 2025


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


def load_key_set(path: Path) -> set:
    keys = set()
    for row in read_jsonl(path):
        key = row.get("_key")
        if isinstance(key, str) and key.strip():
            keys.add(key)
    return keys


def is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_raw_row(row: dict) -> List[str]:
    reasons: List[str] = []
    key = row.get("_key")
    profile = row.get("profile")
    system = row.get("system")
    messages = row.get("messages")

    if not is_nonempty_str(key):
        reasons.append("invalid_key")
    if profile not in VALID_PROFILES:
        reasons.append("invalid_profile")
    if not is_nonempty_str(system):
        reasons.append("invalid_system")
    if not isinstance(messages, list) or len(messages) < 2:
        reasons.append("invalid_messages_list")
        return reasons

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            reasons.append(f"message_not_object_{i}")
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role not in VALID_ROLES:
            reasons.append(f"invalid_role_{i}")
        if not is_nonempty_str(content):
            reasons.append(f"invalid_content_{i}")
    return reasons


def classify_sample_type(key: str, run1_keys: set, normal_keys: set) -> str:
    if key.startswith("_"):
        return "error"
    if key in run1_keys and key not in normal_keys:
        return "error"
    return "normal"


def analyze(rows: List[dict], run1_keys: set, normal_keys: set) -> Dict[str, object]:
    profile_counts = Counter()
    group_counts = Counter()
    error_total = 0
    invalid_rows = []

    for idx, row in enumerate(rows):
        reasons = validate_raw_row(row)
        if reasons:
            invalid_rows.append((idx, row.get("_key"), reasons))
            continue

        profile = row["profile"]
        key = row["_key"]
        sample_type = classify_sample_type(key, run1_keys, normal_keys)
        profile_counts[profile] += 1
        group_counts[(profile, sample_type)] += 1
        if sample_type == "error":
            error_total += 1

    return {
        "total_rows": len(rows),
        "profile_counts": dict(profile_counts),
        "group_counts": dict(group_counts),
        "error_total": error_total,
        "normal_total": len(rows) - error_total,
        "invalid_rows": invalid_rows,
    }


def summarize_split(rows: List[dict], run1_keys: set, normal_keys: set) -> Dict[str, object]:
    profile_counts = Counter()
    error_counts = Counter()
    keys = set()
    turkish_chars = "çğıöşüÇĞİÖŞÜ"
    has_turkish = False
    turkish_example = ""

    for row in rows:
        key = row["_key"]
        keys.add(key)
        profile = row["profile"]
        sample_type = classify_sample_type(key, run1_keys, normal_keys)
        profile_counts[profile] += 1
        error_counts[(profile, sample_type)] += 1

        if not has_turkish:
            for msg in row.get("messages", []):
                content = msg.get("content") if isinstance(msg, dict) else ""
                if isinstance(content, str) and any(ch in content for ch in turkish_chars):
                    has_turkish = True
                    turkish_example = content[:120]
                    break

    total = len(rows)
    error_total = sum(v for (p, t), v in error_counts.items() if t == "error")
    error_ratio = (error_total / total * 100.0) if total else 0.0
    return {
        "total": total,
        "profile_counts": dict(profile_counts),
        "group_counts": dict(error_counts),
        "error_total": error_total,
        "error_ratio": error_ratio,
        "keys": keys,
        "turkish_ok": has_turkish,
        "turkish_example": turkish_example,
    }


def main() -> None:
    rows = read_jsonl(INPUT_PATH)
    run1_keys = load_key_set(RUN1_PASSED_PATH)
    normal_keys = load_key_set(M1_PASS_PATH) | load_key_set(M2_PASS_PATH)

    analysis = analyze(rows, run1_keys, normal_keys)
    invalid_rows = analysis["invalid_rows"]

    print("=== ADIM 1/2: INPUT ANALIZ ===")
    print(f"Toplam kayıt: {analysis['total_rows']}")
    print(f"Profil dağılımı: {analysis['profile_counts']}")
    print(f"Hatalı örnek sayısı: {analysis['error_total']}")
    print(f"Normal örnek sayısı: {analysis['normal_total']}")
    ratio = (analysis["error_total"] / analysis["total_rows"] * 100.0) if analysis["total_rows"] else 0.0
    print(f"Hatalı örnek oranı: %{ratio:.4f}")

    if invalid_rows:
        sample = invalid_rows[:5]
        raise RuntimeError(f"Geçersiz raw kayıt bulundu: count={len(invalid_rows)} sample={sample}")

    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in rows:
        profile = row["profile"]
        sample_type = classify_sample_type(row["_key"], run1_keys, normal_keys)
        grouped[(profile, sample_type)].append(row)

    group_order = [
        ("FAST", "normal"),
        ("FAST", "error"),
        ("MEM", "normal"),
        ("MEM", "error"),
        ("SLOW", "normal"),
        ("SLOW", "error"),
    ]

    train_rows: List[dict] = []
    val_rows: List[dict] = []

    print("\n=== ADIM 3: STRATIFIED SPLIT ===")
    for group_key in group_order:
        group = list(grouped.get(group_key, []))
        rng = random.Random(SEED)
        rng.shuffle(group)
        n = len(group)
        val_n = int(n * VAL_RATIO)  # floor
        train_n = n - val_n
        val_group = group[:val_n]
        train_group = group[val_n:]
        train_rows.extend(train_group)
        val_rows.extend(val_group)
        print(f"{group_key[0]} + {group_key[1]}: total={n} train={train_n} val={val_n}")

    write_jsonl(TRAIN_OUT, train_rows)
    write_jsonl(VAL_OUT, val_rows)

    train_stats = summarize_split(train_rows, run1_keys, normal_keys)
    val_stats = summarize_split(val_rows, run1_keys, normal_keys)
    overlap = train_stats["keys"] & val_stats["keys"]
    total_check = train_stats["total"] + val_stats["total"]

    print("\n=== ADIM 5: FINAL DOGRULAMA ===")
    print(f"train_raw.jsonl: {train_stats['total']} satır")
    print(
        "  FAST: {0} (normal: {1}, error: {2})".format(
            train_stats["profile_counts"].get("FAST", 0),
            train_stats["group_counts"].get(("FAST", "normal"), 0),
            train_stats["group_counts"].get(("FAST", "error"), 0),
        )
    )
    print(
        "  MEM: {0} (normal: {1}, error: {2})".format(
            train_stats["profile_counts"].get("MEM", 0),
            train_stats["group_counts"].get(("MEM", "normal"), 0),
            train_stats["group_counts"].get(("MEM", "error"), 0),
        )
    )
    print(
        "  SLOW: {0} (normal: {1}, error: {2})".format(
            train_stats["profile_counts"].get("SLOW", 0),
            train_stats["group_counts"].get(("SLOW", "normal"), 0),
            train_stats["group_counts"].get(("SLOW", "error"), 0),
        )
    )
    print(f"  Toplam error: {train_stats['error_total']} (%{train_stats['error_ratio']:.4f})")

    print(f"val_raw.jsonl: {val_stats['total']} satır")
    print(
        "  FAST: {0} (normal: {1}, error: {2})".format(
            val_stats["profile_counts"].get("FAST", 0),
            val_stats["group_counts"].get(("FAST", "normal"), 0),
            val_stats["group_counts"].get(("FAST", "error"), 0),
        )
    )
    print(
        "  MEM: {0} (normal: {1}, error: {2})".format(
            val_stats["profile_counts"].get("MEM", 0),
            val_stats["group_counts"].get(("MEM", "normal"), 0),
            val_stats["group_counts"].get(("MEM", "error"), 0),
        )
    )
    print(
        "  SLOW: {0} (normal: {1}, error: {2})".format(
            val_stats["profile_counts"].get("SLOW", 0),
            val_stats["group_counts"].get(("SLOW", "normal"), 0),
            val_stats["group_counts"].get(("SLOW", "error"), 0),
        )
    )
    print(f"  Toplam error: {val_stats['error_total']} (%{val_stats['error_ratio']:.4f})")

    print(f"Toplam: {total_check} (beklenen {EXPECTED_TOTAL})")
    print(f"Overlap: {len(overlap)} key (beklenen 0)")
    print(
        "Türkçe karakter: train={0}, val={1}".format(
            "OK" if train_stats["turkish_ok"] else "FAIL",
            "OK" if val_stats["turkish_ok"] else "FAIL",
        )
    )

    if total_check != EXPECTED_TOTAL:
        raise RuntimeError(f"Toplam kayıt hatası: {total_check} != {EXPECTED_TOTAL}")
    if len(overlap) != 0:
        raise RuntimeError(f"Train/Val key overlap bulundu: {len(overlap)}")
    if not train_stats["turkish_ok"] or not val_stats["turkish_ok"]:
        raise RuntimeError("Türkçe karakter kontrolü başarısız")

    expected_train = 1824
    expected_val = 201
    if train_stats["total"] != expected_train or val_stats["total"] != expected_val:
        raise RuntimeError(
            f"Split sayıları beklenenle uyuşmuyor: train={train_stats['total']} val={val_stats['total']}"
        )

    print("\nSplit tamamlandı.")
    print(f"-> {TRAIN_OUT}")
    print(f"-> {VAL_OUT}")


if __name__ == "__main__":
    main()
