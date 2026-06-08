import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


KEY_SOURCES: List[Tuple[Path, Path, str]] = [
    (Path("Maverick/mv_pass.jsonl"), Path("dialogs/passed.jsonl"), "Maverick"),
    (Path("Maverick2/mv_pass.jsonl"), Path("dialogs2/passed.jsonl"), "Maverick2"),
    (Path("error_dialogs_run1/passed.jsonl"), Path("error_dialogs_run1/generated.jsonl"), "run1"),
]

OUTPUT_PATH = Path("all_passed_with_dialogs.jsonl")
EXPECTED_TOTAL = 2025
MINIMAL_FIELDS = ["_key", "profile", "topic", "source_question", "system", "messages"]
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


def build_dialog_map(rows: List[dict], source_path: Path) -> Tuple[Dict[str, dict], int]:
    dialog_map: Dict[str, dict] = {}
    duplicate_keys = 0
    for row in rows:
        key = str(row.get("_key", "")).strip()
        if not key:
            continue
        if key in dialog_map:
            duplicate_keys += 1
            continue
        dialog_map[key] = row
    return dialog_map, duplicate_keys


def to_minimal_row(row: dict) -> dict:
    out = {field: row.get(field) for field in MINIMAL_FIELDS}
    return out


def is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_output(rows: List[dict]) -> dict:
    issues = Counter()
    unique_keys = set()

    for row in rows:
        key = row.get("_key")
        if not is_nonempty_string(key):
            issues["invalid_key"] += 1
        else:
            unique_keys.add(key)

        if row.get("profile") not in {"FAST", "MEM", "SLOW"}:
            issues["invalid_profile"] += 1
        if not is_nonempty_string(row.get("topic")):
            issues["invalid_topic"] += 1
        if not is_nonempty_string(row.get("source_question")):
            issues["invalid_source_question"] += 1
        if not is_nonempty_string(row.get("system")):
            issues["invalid_system"] += 1

        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            issues["invalid_messages_list"] += 1
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                issues["message_not_dict"] += 1
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role not in VALID_ROLES:
                issues["invalid_role"] += 1
            if not is_nonempty_string(content):
                issues["invalid_content"] += 1

    return {
        "row_count": len(rows),
        "unique_key_count": len(unique_keys),
        "issues": dict(issues),
    }


def main() -> None:
    output_rows: List[dict] = []
    used_keys = set()
    missing_keys: List[Tuple[str, str]] = []
    source_stats = {}

    for eval_path, dialog_path, source_name in KEY_SOURCES:
        eval_rows = read_jsonl(eval_path)
        dialog_rows = read_jsonl(dialog_path)
        dialog_map, duplicate_dialog_keys = build_dialog_map(dialog_rows, dialog_path)

        collected = 0
        renamed = 0

        for eval_row in eval_rows:
            base_key = str(eval_row.get("_key", "")).strip()
            if not base_key:
                continue

            dialog_row = dialog_map.get(base_key)
            if dialog_row is None:
                missing_keys.append((source_name, base_key))
                continue

            out = to_minimal_row(dialog_row)
            out_key = base_key
            while out_key in used_keys:
                out_key = "_" + out_key
            if out_key != base_key:
                renamed += 1
            out["_key"] = out_key
            output_rows.append(out)
            used_keys.add(out_key)
            collected += 1

        source_stats[source_name] = {
            "eval_rows": len(eval_rows),
            "dialog_rows": len(dialog_rows),
            "duplicate_dialog_keys": duplicate_dialog_keys,
            "collected_rows": collected,
            "renamed_due_to_collision": renamed,
        }

    write_jsonl(OUTPUT_PATH, output_rows)
    validation = validate_output(output_rows)

    print("=== PASS + DIALOG MERGE SUMMARY ===")
    for source_name in ("Maverick", "Maverick2", "run1"):
        stats = source_stats[source_name]
        print(
            f"{source_name}: eval={stats['eval_rows']} dialog={stats['dialog_rows']} "
            f"collected={stats['collected_rows']} renamed={stats['renamed_due_to_collision']} "
            f"dialog_dup_keys={stats['duplicate_dialog_keys']}"
        )

    print(f"missing_keys={len(missing_keys)}")
    print(f"output_rows={validation['row_count']}")
    print(f"unique_keys={validation['unique_key_count']}")
    print(f"issues={validation['issues']}")
    print(f"expected_total={EXPECTED_TOTAL}")
    print(f"output_file={OUTPUT_PATH}")

    if missing_keys:
        sample = ", ".join(f"{src}:{key}" for src, key in missing_keys[:10])
        print(f"missing_sample={sample}")

    if validation["row_count"] != EXPECTED_TOTAL:
        raise RuntimeError(
            f"Beklenen toplam {EXPECTED_TOTAL}, bulunan {validation['row_count']}"
        )
    if validation["row_count"] != validation["unique_key_count"]:
        raise RuntimeError("Final _key benzersiz değil")
    if validation["issues"]:
        raise RuntimeError(f"Validasyon hataları var: {validation['issues']}")
    if missing_keys:
        raise RuntimeError("Eksik key bulundu")


if __name__ == "__main__":
    main()
