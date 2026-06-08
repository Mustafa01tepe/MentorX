import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


TRAIN_RAW = Path("train_raw.jsonl")
VAL_RAW = Path("val_raw.jsonl")

TRAIN_OUT = Path("train_v2.jsonl")
VAL_OUT = Path("val_v2.jsonl")
INVALID_OUT = Path("chatml_invalid_v2.jsonl")

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


def is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_raw_for_chatml(row: dict) -> List[str]:
    reasons: List[str] = []
    if not is_nonempty_str(row.get("system")):
        reasons.append("invalid_system")

    messages = row.get("messages")
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
            reasons.append(f"empty_content_{i}")
    return reasons


def validate_chatml(row: dict) -> List[str]:
    reasons: List[str] = []
    if set(row.keys()) != {"messages"}:
        reasons.append("extra_or_missing_top_level_fields")

    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 3:
        reasons.append("invalid_chatml_messages")
        return reasons

    if messages[0].get("role") != "system":
        reasons.append("first_not_system")
    if messages[1].get("role") != "user":
        reasons.append("second_not_user")
    if messages[-1].get("role") != "assistant":
        reasons.append("last_not_assistant")

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            reasons.append(f"chatml_message_not_object_{i}")
            continue
        if not is_nonempty_str(msg.get("content")):
            reasons.append(f"chatml_empty_content_{i}")
        role = msg.get("role")
        if i == 0:
            if role != "system":
                reasons.append("system_role_invalid")
            continue
        expected = "user" if i % 2 == 1 else "assistant"
        if role != expected:
            reasons.append(f"not_alternating_{i}")
            break

    return reasons


def convert_split(split_name: str, rows: List[dict]) -> Tuple[List[dict], List[dict], Counter]:
    valid_rows: List[dict] = []
    invalid_rows: List[dict] = []
    reason_counts: Counter = Counter()

    for idx, row in enumerate(rows):
        raw_reasons = validate_raw_for_chatml(row)
        if raw_reasons:
            reason_counts.update(set(raw_reasons))
            invalid_rows.append(
                {
                    "_split": split_name,
                    "_index": idx,
                    "_key": row.get("_key"),
                    "reasons": sorted(set(raw_reasons)),
                    "record": row,
                }
            )
            continue

        chatml_row = {
            "messages": [{"role": "system", "content": row["system"]}] + row["messages"],
        }
        qc_reasons = validate_chatml(chatml_row)
        if qc_reasons:
            reason_counts.update(set(qc_reasons))
            invalid_rows.append(
                {
                    "_split": split_name,
                    "_index": idx,
                    "_key": row.get("_key"),
                    "reasons": sorted(set(qc_reasons)),
                    "record": row,
                }
            )
            continue

        valid_rows.append(chatml_row)

    return valid_rows, invalid_rows, reason_counts


def final_validate_output(path: Path) -> Tuple[int, bool]:
    total = 0
    ok = True
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                ok = False
                continue
            reasons = validate_chatml(row)
            if reasons:
                ok = False
    return total, ok


def find_turkish_example(paths: List[Path]) -> Tuple[bool, str]:
    chars = "çğıöşüÇĞİÖŞÜ"
    for path in paths:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                for msg in row.get("messages", []):
                    content = msg.get("content")
                    if isinstance(content, str) and any(ch in content for ch in chars):
                        return True, content[:120]
    return False, ""


def main() -> None:
    train_raw_rows = read_jsonl(TRAIN_RAW)
    val_raw_rows = read_jsonl(VAL_RAW)

    print("=== ADIM 1: DOSYALARI YUKLE ===")
    print(f"train_raw.jsonl: {len(train_raw_rows)} kayıt yüklendi")
    print(f"val_raw.jsonl: {len(val_raw_rows)} kayıt yüklendi")

    train_valid, train_invalid, train_reasons = convert_split("train", train_raw_rows)
    val_valid, val_invalid, val_reasons = convert_split("val", val_raw_rows)
    all_invalid = train_invalid + val_invalid
    all_reason_counts = train_reasons + val_reasons

    print("\n=== ADIM 3: KALITE KONTROL ===")
    print(f"Train geçerli: {len(train_valid)} kayıt")
    print(f"Train geçersiz: {len(train_invalid)} kayıt")
    print(f"Val geçerli: {len(val_valid)} kayıt")
    print(f"Val geçersiz: {len(val_invalid)} kayıt")
    print(f"Geçersiz neden dağılımı: {dict(sorted(all_reason_counts.items()))}")

    write_jsonl(TRAIN_OUT, train_valid)
    write_jsonl(VAL_OUT, val_valid)
    write_jsonl(INVALID_OUT, all_invalid)

    train_lines, train_ok = final_validate_output(TRAIN_OUT)
    val_lines, val_ok = final_validate_output(VAL_OUT)

    turkish_ok, turkish_example = find_turkish_example([TRAIN_OUT, VAL_OUT])
    raw_total = len(train_raw_rows) + len(val_raw_rows)
    out_total = train_lines + val_lines
    expected_out_total = raw_total - len(all_invalid)
    totals_match = out_total == expected_out_total

    print("\n=== ADIM 5: FINAL DOGRULAMA ===")
    print(f"train_v2.jsonl: {train_lines} satır {'✓' if train_ok else '✗'}")
    print(f"val_v2.jsonl: {val_lines} satır {'✓' if val_ok else '✗'}")
    if turkish_ok:
        print(f"Türkçe karakter örnek: {turkish_example} OK")
    else:
        print("Türkçe karakter örnek: FAIL")
    print(
        f"Toplam kontrolü: {out_total} == {expected_out_total} "
        f"{'✓' if totals_match else '✗'}"
    )

    if not train_ok or not val_ok:
        raise RuntimeError("ChatML final doğrulama başarısız")
    if not turkish_ok:
        raise RuntimeError("Türkçe karakter kontrolü başarısız")
    if not totals_match:
        raise RuntimeError(
            f"Toplam satır uyuşmuyor: out={out_total}, expected={expected_out_total}"
        )

    print("\nDönüşüm tamamlandı.")
    print(f"-> {TRAIN_OUT}")
    print(f"-> {VAL_OUT}")
    print(f"-> {INVALID_OUT}")


if __name__ == "__main__":
    main()
