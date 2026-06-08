"""
check_coverage.py
─────────────────
keys.jsonl + question_bank.json → hangi sorular tam üretilmiş, hangilerinde eksik profil var?

Çıktı: coverage.jsonl
"""

import json
from pathlib import Path
from collections import defaultdict

KEYS_FILE     = Path("keys.jsonl")
QB_FILE       = Path("Other Versions/question_bank.json")
OUTPUT_FILE   = Path("coverage.jsonl")
PROFILES      = ["FAST", "MEM", "SLOW"]

def main():
    # 1. question_bank yükle
    with open(QB_FILE, encoding="utf-8") as f:
        questions = json.load(f)

    # 2. keys.jsonl'den qi → profil eşlemesi
    produced = defaultdict(set)   # qi → {FAST, MEM, ...}
    with open(KEYS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = rec.get("key", "")
            # "714_MEM" → qi=714, profile=MEM
            parts = key.rsplit("_", 1)
            if len(parts) == 2 and parts[0].isdigit():
                qi      = int(parts[0])
                profile = parts[1]
                produced[qi].add(profile)

    # 3. Her soru için coverage hesapla
    total    = len(questions)
    full     = 0
    partial  = 0
    empty    = 0

    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        for qi, q in enumerate(questions):
            done    = produced.get(qi, set())
            missing = [p for p in PROFILES if p not in done]

            status = "tam" if not missing else ("kısmi" if done else "üretilmedi")

            if status == "tam":      full    += 1
            elif status == "kısmi":  partial += 1
            else:                    empty   += 1

            record = {
                "qi":         qi,
                "difficulty": q.get("Difficulty", ""),
                "category":   q.get("Category", ""),
                "type":       q.get("Type", ""),
                "FAST":       "FAST" in done,
                "MEM":        "MEM"  in done,
                "SLOW":       "SLOW" in done,
                "missing":    missing,
                "status":     status,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 4. Özet
    print(f"=== COVERAGE RAPORU ===")
    print(f"Toplam soru     : {total}")
    print(f"✅ Tam (3/3)    : {full}")
    print(f"⚠️  Kısmi        : {partial}")
    print(f"❌ Üretilmedi   : {empty}")
    print(f"\n→ {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
