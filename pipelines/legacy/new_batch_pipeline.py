"""
new_batch_pipeline.py
─────────────────────
DeepSeek API ile eksik diyalogları üretir.
Girdi  : coverage.jsonl + question_bank.json
Çıktı  : dialogs2/passed.jsonl | dialogs2/failed.jsonl

Üretim sırası:
  1. status=üretilmedi (hiç üretilmemiş sorular)
  2. status=kısmi      (eksik profiller)
"""

import json
import os
import asyncio
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# AYARLAR
# ---------------------------------------------------------------------------
COVERAGE_FILE   = Path("coverage.jsonl")
QB_FILE         = Path("Other Versions") / "question_bank.json"
OUTPUT_DIR      = Path("dialogs3")
OUTPUT_DIR.mkdir(exist_ok=True)

PASSED_FILE     = OUTPUT_DIR / "passed.jsonl"
FAILED_FILE     = OUTPUT_DIR / "failed.jsonl"

MODEL           = "deepseek-chat"
MAX_RETRIES     = 3
RETRY_DELAY     = 30
CALL_TIMEOUT    = 120
CONCURRENCY     = 4
MAX_TOKENS      = 7000
VALID_PROFILES  = {"FAST", "MEM", "SLOW"}
TARGET_STATUSES = {"üretilmedi", "kısmi"}

# ---------------------------------------------------------------------------
# MİNİMUM TUR MATRİSİ (profil × zorluk) — üstü serbest
# ---------------------------------------------------------------------------
MIN_TURN_MATRIX = {
    "FAST": {"Level 1": 3, "Level 2": 4, "Level 3": 5},
    "MEM":  {"Level 1": 4, "Level 2": 5, "Level 3": 6},
    "SLOW": {"Level 1": 5, "Level 2": 6, "Level 3": 7},
}
DEFAULT_MIN_TURNS = {"FAST": 4, "MEM": 5, "SLOW": 6}

def get_min_turns(profile, difficulty):
    return MIN_TURN_MATRIX.get(profile, {}).get(difficulty, DEFAULT_MIN_TURNS.get(profile, 4))

# ---------------------------------------------------------------------------
# TEMPERATURE MATRİSİ (profil × zorluk)
# ---------------------------------------------------------------------------
TEMPERATURE_MATRIX = {
    "FAST": {"Level 1": 0.2, "Level 2": 0.2, "Level 3": 0.25},
    "MEM":  {"Level 1": 0.2, "Level 2": 0.2, "Level 3": 0.25},
    "SLOW": {"Level 1": 0.3, "Level 2": 0.3, "Level 3": 0.5},
}
DEFAULT_TEMP = {"FAST": 0.2, "MEM": 0.2, "SLOW": 0.3}

def get_temperature(profile, difficulty):
    return TEMPERATURE_MATRIX.get(profile, {}).get(difficulty, DEFAULT_TEMP.get(profile, 0.3))

# ---------------------------------------------------------------------------
# PROFİL TANIMLARI
# ---------------------------------------------------------------------------
PROFILE_DESCRIPTIONS = {
    "FAST": (
        "Öğrenci konuya hakimdir, hızlı ilerler, bağlantıları kendisi kurar. "
        "TUTOR: Boş onay ve tanım tekrarı sıfır. "
        "Mantık sapmasını tek cümlede yakalayıp öğrenciye aynala. "
        "Derin sorular sor, karşılaştırma ve genelleme iste."
    ),
    "MEM": (
        "Öğrenci tanımı ezberden bilir ama anlamını kavrayamaz, transfer yapamaz. "
        "TUTOR: Formül ve kuralları kabul etme, kavramsal kökeni sor. "
        "'Neden?' sorusu sor, örnek ve karşı örnek zorla, "
        "indirgemenin neden iki yönlü çalışması gerektiğini sorgulatma."
    ),
    "SLOW": (
        "Öğrenci temelde zorlanır, kararsızdır, adım adım ilerlemesi gerekir. "
        "TUTOR: Adımları mikro seviyeye indir, acele ettirme. "
        "Somut örnekler ver, karmaşık soyut ispatlar için "
        "esnek analogiler (domino taşı, labirent gibi) kullan."
    ),
}

SYSTEM_PROMPT = (
    "Sen MentorX adlı bir Türkçe Otomata Teorisi tutorusun. "
    "Öğrenciye asla doğrudan cevap vermezsin, "
    "her mesajında yönlendirici sorular sorarsın, "
    "kısa ve öz konuşursun."
)

# ---------------------------------------------------------------------------
# PROMPT BUILDER
# ---------------------------------------------------------------------------
def build_prompt(q, profile, min_turns):
    answer_text = q.get("Answer", "") or ""
    answer_ref  = answer_text[:800] if answer_text else "Cevap mevcut değil, sorudan çıkar."

    return f"""Sen MentorX adlı bir Türkçe Socratic Automata Theory tutor sistemi için eğitim verisi üretiyorsun.

## GÖREV
Aşağıdaki soruyu kullanarak Türkçe, çok turlu, Socratic bir öğretmen-öğrenci diyalogu üret.

## SORU
{q["Question"]}

## DOĞRU CEVAP (referans — diyalogda ASLA doğrudan verme)
{answer_ref}

## ÖĞRENCİ PROFİLİ: {profile}
{PROFILE_DESCRIPTIONS[profile]}

## ZORUNLU KURALLAR

### ⚠️ TUR KURALI
- 1 tur = 1 user mesajı + 1 assistant mesajı
- Diyalog EN AZ {min_turns} turdan oluşmalıdır
- Sorunun karmaşıklığına göre daha uzun olabilir (üst sınır yok)
- Diyalog yarıda ASLA kesilmemeli

### Diyalog akışı
- İlk turlarda tutor sorularla öğrenciyi yönlendirir, öğrenci adım adım ilerler
- Son turda öğrenci sonuca ulaşır, tutor konuyu kapatır, yeni soru açmaz

### Tutor mesaj kuralları
- Her tutor mesajında EN AZ 1 yönlendirici soru (? işareti olmalı)
- Tutor ASLA doğrudan cevap vermez
- Tutor ASLA "Harika!", "Mükemmel!", "Doğru!" gibi boş onaylar vermez
- Tutor son mesajında konuyu kapatır, yeni soru açmaz

### Dil kuralları
- Diyalog tamamen Türkçe olacak
- Diyalog USER ile başlar, ASSISTANT ile biter

### Profil uyumu
- Öğrenci {profile} profiline uygun konuşur
- Tutor davranışı da profile göre ayarlanır

## FORMAT (sadece bu JSON'u döndür, başka hiçbir şey yazma)
{{
  "profile": "{profile}",
  "topic": "konu adı",
  "source_question": "orijinal soru metni",
  "system": "{SYSTEM_PROMPT}",
  "messages": [
    {{"role": "user", "content": "..."}},
    {{"role": "assistant", "content": "..."}},
    {{"role": "user", "content": "..."}},
    {{"role": "assistant", "content": "..."}}
  ]
}}"""

# ---------------------------------------------------------------------------
# JSON TEMİZLE
# ---------------------------------------------------------------------------
def clean_json(raw: str) -> str:
    raw = raw.strip()
    # Backtick bloğu varsa içini al
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
    raw = raw.strip()
    # İlk { ile son } arasını al
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        return raw[start:end+1]
    return raw

# ---------------------------------------------------------------------------
# YAPISAL VALIDATOR
# ---------------------------------------------------------------------------
def validate(dialog, min_turns):
    errors   = []
    messages = dialog.get("messages", [])

    if not messages:
        return ["messages boş"]

    # Boş içerik kontrolü
    for i, msg in enumerate(messages):
        content = msg.get("content", "").strip()
        if not content or content == "...":
            errors.append(f"Mesaj {i+1}: boş içerik")

    # İlk / son mesaj kontrolü
    if messages[0].get("role") != "user":
        errors.append("İlk mesaj user değil")
    if messages[-1].get("role") != "assistant":
        errors.append("Son mesaj assistant değil")

    # Sıra kontrolü
    for i, msg in enumerate(messages):
        expected = "user" if i % 2 == 0 else "assistant"
        if msg.get("role") != expected:
            errors.append(f"Mesaj {i+1}: beklenen {expected}, gelen {msg.get('role')}")

    # Minimum tur kontrolü
    actual_turns = len(messages) // 2
    if actual_turns < min_turns:
        errors.append(f"Tur sayısı yetersiz: {actual_turns} (minimum {min_turns})")

    # Tutor mesaj kuralları (son mesaj hariç soru zorunlu)
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        if i < len(messages) - 1 and "?" not in msg.get("content", ""):
            errors.append(f"Tutor mesaj {i+1}: yönlendirici soru yok (?)")

    return errors

# ---------------------------------------------------------------------------
# API ÇAĞRISI
# ---------------------------------------------------------------------------
async def api_call(client, api_key, prompt, temperature):
    url     = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model":       MODEL,
        "temperature": temperature,
        "max_tokens":  MAX_TOKENS,
        "messages":    [{"role": "user", "content": prompt}]
    }

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]

            status = resp.status_code
            err    = resp.text.lower()

            if status == 429 or "rate limit" in err:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"    🚫 429 — {wait}s bekleniyor...")
                await asyncio.sleep(wait)
            elif status == 503 or "service unavailable" in err or "too busy" in err:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"    🔴 503 — {wait}s bekleniyor...")
                await asyncio.sleep(wait)
            elif status == 500 or "internal server error" in err:
                wait = 15 * (attempt + 1)
                print(f"    🔴 500 — {wait}s bekleniyor...")
                await asyncio.sleep(wait)
            else:
                print(f"    ⚠️ HTTP {status} ({attempt+1}): {resp.text[:100]}")
                await asyncio.sleep(10 * (attempt + 1))

        except httpx.TimeoutException:
            print(f"    ⏱ Timeout — {15*(attempt+1)}s bekleniyor...")
            await asyncio.sleep(15 * (attempt + 1))
        except Exception as e:
            print(f"    ⚠️ Hata ({attempt+1}): {e}")
            await asyncio.sleep(10 * (attempt + 1))

    raise RuntimeError("API max retry aşıldı")

# ---------------------------------------------------------------------------
# TEK KAYIT ÜRETİMİ
# ---------------------------------------------------------------------------
async def process_one(qi, profile, q, client, api_key, sem, files, lock, stats):
    key         = f"{qi}_{profile}"
    difficulty  = q.get("Difficulty", "Level 2")
    min_turns   = get_min_turns(profile, difficulty)
    temperature = get_temperature(profile, difficulty)

    async with sem:
        try:
            prompt = build_prompt(q, profile, min_turns)
            raw    = await api_call(client, api_key, prompt, temperature)
            dialog = json.loads(clean_json(raw))
            errors = validate(dialog, min_turns)

            record = {
                "_key":    key,
                "_qi":     qi,
                "_errors": errors,
                **dialog
            }

            async with lock:
                if not errors:
                    files["pass"].write(json.dumps(record, ensure_ascii=False) + "\n")
                    files["pass"].flush()
                    stats["passed"] += 1
                    icon = "✅"
                else:
                    files["fail"].write(json.dumps(record, ensure_ascii=False) + "\n")
                    files["fail"].flush()
                    stats["failed"] += 1
                    icon = "❌"

                total = stats["passed"] + stats["failed"]
                print(f"[{total}] {icon} {profile} | Q{qi} | {difficulty} | {q['Question'][:50]}")
                if errors:
                    for e in errors:
                        print(f"      - {e}")

        except Exception as e:
            async with lock:
                record = {"_key": key, "_qi": qi, "_errors": [str(e)], "profile": profile}
                files["fail"].write(json.dumps(record, ensure_ascii=False) + "\n")
                files["fail"].flush()
                stats["failed"] += 1
                print(f"[ERR] ❌ {profile} | Q{qi} | {e}")

# ---------------------------------------------------------------------------
# RESUME DESTEĞİ
# ---------------------------------------------------------------------------
def load_done():
    done = set()
    for f in [PASSED_FILE, FAILED_FILE]:
        if f.exists():
            with open(f, encoding="utf-8") as fp:
                for line in fp:
                    try:
                        key = json.loads(line).get("_key", "")
                        if key:
                            done.add(key)
                    except:
                        pass
    return done

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main():
    with open(QB_FILE, encoding="utf-8") as f:
        questions = json.load(f)

    done             = load_done()
    done_before_count = len(done)
    api_key          = os.getenv("DEEPSEEK_API_KEY")
    sem              = asyncio.Semaphore(CONCURRENCY)
    lock             = asyncio.Lock()
    stats            = {"passed": 0, "failed": 0, "skipped_invalid": 0}

    with open(PASSED_FILE, "a", encoding="utf-8") as pf, \
         open(FAILED_FILE, "a", encoding="utf-8") as ff:

        files = {"pass": pf, "fail": ff}

        def write_invalid_record(line_no, errors, qi=None, profile=None, raw_line=None):
            qi_for_key      = qi if isinstance(qi, int) else "NA"
            profile_for_key = profile if isinstance(profile, str) and profile else "NA"
            key = f"__invalid__line{line_no}__qi{qi_for_key}__profile{profile_for_key}"
            if key in done:
                return
            record = {
                "_key":            key,
                "_qi":             qi if isinstance(qi, int) else None,
                "_errors":         errors,
                "profile":         profile if isinstance(profile, str) else "UNKNOWN",
                "_coverage_line":  line_no,
            }
            if raw_line:
                record["_coverage_raw"] = raw_line[:400]
            files["fail"].write(json.dumps(record, ensure_ascii=False) + "\n")
            files["fail"].flush()
            done.add(key)
            stats["skipped_invalid"] += 1
            print(f"[SKIP] ⚠️ line {line_no} | {'; '.join(errors)}")

        # Üretim sırası: önce üretilmedi, sonra kısmi
        todo_by_status = {"üretilmedi": [], "kısmi": []}

        with open(COVERAGE_FILE, encoding="utf-8") as cf:
            for line_no, line in enumerate(cf, start=1):
                if not line.strip():
                    continue

                raw_line = line.strip()
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    write_invalid_record(line_no=line_no, errors=[f"JSON parse hatası: {e.msg}"], raw_line=raw_line)
                    continue

                if not isinstance(rec, dict):
                    write_invalid_record(line_no=line_no, errors=["Kayıt dict değil"], raw_line=raw_line)
                    continue

                missing_fields = [k for k in ("status", "missing", "qi") if k not in rec]
                if missing_fields:
                    write_invalid_record(line_no=line_no, errors=[f"Eksik alan: {', '.join(missing_fields)}"], raw_line=raw_line)
                    continue

                status = rec["status"]
                if status not in TARGET_STATUSES:
                    continue

                missing = rec["missing"]
                if not isinstance(missing, list) or not missing:
                    write_invalid_record(line_no=line_no, errors=["'missing' liste değil veya boş"], qi=rec.get("qi"), raw_line=raw_line)
                    continue

                qi = rec["qi"]
                if not isinstance(qi, int):
                    write_invalid_record(line_no=line_no, errors=["'qi' integer değil"], qi=qi, raw_line=raw_line)
                    continue

                if qi < 0 or qi >= len(questions):
                    write_invalid_record(line_no=line_no, errors=[f"'qi' aralık dışı: {qi}"], qi=qi, raw_line=raw_line)
                    continue

                seen_profiles = set()
                for profile in missing:
                    if not isinstance(profile, str) or profile not in VALID_PROFILES:
                        write_invalid_record(line_no=line_no, errors=[f"Bilinmeyen profil: {profile!r}"], qi=qi, profile=str(profile), raw_line=raw_line)
                        continue
                    if profile in seen_profiles:
                        continue
                    seen_profiles.add(profile)
                    todo_by_status[status].append((qi, profile, questions[qi]))

        todo = todo_by_status["üretilmedi"] + todo_by_status["kısmi"]
        todo = [(qi, p, q) for qi, p, q in todo if f"{qi}_{p}" not in done]

        print(f"=== YENİ BATCH PİPELİNE (DeepSeek) ===")
        print(f"Üretilecek      : {len(todo)} diyalog")
        print(f"Atlanıyor (done): {done_before_count}")
        print(f"Invalid kayıt   : {stats['skipped_invalid']}")
        print()

        async with httpx.AsyncClient(timeout=CALL_TIMEOUT) as client:
            await asyncio.gather(*[
                asyncio.create_task(
                    process_one(qi, profile, q, client, api_key, sem, files, lock, stats)
                )
                for qi, profile, q in todo
            ])

    print()
    print("=== SONUÇ ===")
    print(f"✅ Geçti     : {stats['passed']}")
    print(f"❌ Başarısız : {stats['failed']}")
    print(f"⚠️ Invalid   : {stats['skipped_invalid']}")
    print(f"→ {PASSED_FILE} | {FAILED_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
