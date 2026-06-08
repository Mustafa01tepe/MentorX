"""
merge_to_sft.py
Build a single SFT dataset from all judged "pass" dialogues.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

SOURCES = [
    {
        "name": "Maverick",
        "pass_file": Path("Maverick/mv_pass.jsonl"),
        "dialogues_file": Path("dialogs/passed_clean.jsonl"),
    },
    {
        "name": "Maverick2",
        "pass_file": Path("Maverick2/mv_pass.jsonl"),
        "dialogues_file": Path("dialogs2/passed.jsonl"),
    },
]

OUTPUT_JSONL = Path("sft_final.jsonl")
OUTPUT_JSON = Path("sft_final.json")


def load_jsonl(path: Path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  ! JSON parse hatası ({path}:{line_no}): {exc}")
    return records


def to_sft_record(dialog: dict, source_name: str) -> dict:
    system_content = dialog.get("system", "")
    raw_messages = dialog.get("messages", [])

    sft_messages = []
    if system_content:
        sft_messages.append({"role": "system", "content": system_content})
    sft_messages.extend(raw_messages)

    return {
        "_key": dialog.get("_key", ""),
        "_qi": dialog.get("_qi"),
        "profile": dialog.get("profile", ""),
        "topic": dialog.get("topic", ""),
        "source_question": dialog.get("source_question", ""),
        "judge_source": source_name,
        "messages": sft_messages,
    }


def main():
    print("=== SFT FINAL MERGE ===")
    merged_records = []
    seen_keys = set()
    profiles = Counter()
    per_source = defaultdict(Counter)

    for source in SOURCES:
        name = source["name"]
        pass_file = source["pass_file"]
        dialogues_file = source["dialogues_file"]

        print(f"\n[{name}]")
        print(f"- pass:      {pass_file}")
        print(f"- dialogues: {dialogues_file}")

        pass_records = load_jsonl(pass_file)
        dialogues = load_jsonl(dialogues_file)

        pass_keys = [r.get("_key", "") for r in pass_records if r.get("_key")]
        dialogue_by_key = {d.get("_key", ""): d for d in dialogues if d.get("_key")}

        missing_in_dialogues = 0
        duplicate_global = 0

        for key in pass_keys:
            dialog = dialogue_by_key.get(key)
            if dialog is None:
                missing_in_dialogues += 1
                continue
            if key in seen_keys:
                duplicate_global += 1
                continue

            sft_rec = to_sft_record(dialog, name)
            merged_records.append(sft_rec)
            seen_keys.add(key)
            profile = sft_rec.get("profile", "?")
            profiles[profile] += 1
            per_source[name][profile] += 1

        print(f"  pass kayıtları      : {len(pass_keys)}")
        print(f"  eşleşen             : {len(pass_keys) - missing_in_dialogues - duplicate_global}")
        print(f"  dialogues'ta yok    : {missing_in_dialogues}")
        print(f"  global tekrar key   : {duplicate_global}")

    with open(OUTPUT_JSONL, "w", encoding="utf-8") as out_jsonl:
        for rec in merged_records:
            out_jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as out_json:
        json.dump(merged_records, out_json, ensure_ascii=False, indent=2)

    print("\n=== SONUÇ ===")
    print(f"Toplam final kayıt: {len(merged_records)}")
    print("Profil dağılımı:")
    for profile, count in sorted(profiles.items()):
        print(f"  {profile}: {count}")

    print("\nKaynak bazlı profil dağılımı:")
    for src_name in SOURCES:
        name = src_name["name"]
        if not per_source[name]:
            continue
        row = ", ".join(f"{p}={c}" for p, c in sorted(per_source[name].items()))
        print(f"  {name}: {row}")

    print(f"\n→ {OUTPUT_JSONL}")
    print(f"→ {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
