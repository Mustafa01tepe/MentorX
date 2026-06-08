"""
judge_maverick.py
─────────────────
OpenRouter (gpt-oss-120b) ile değerlendirme yapar.
Her diyalog için: critic → verdict → dosyaya yaz

Çıktılar:
  mv_results.jsonl      → tüm sonuçlar
  mv_pass.jsonl         → pass
  mv_review.jsonl       → review
  mv_fail.jsonl         → fail
  mv_errors.jsonl       → pipeline hataları
  mv_disagreement.jsonl → LLM ≠ rule engine olanlar
"""

import json, os, time, asyncio
from pathlib import Path
from collections import Counter, defaultdict
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# AYARLAR
# ---------------------------------------------------------------------------
INPUT_FILE   = "dialogs2/passed.jsonl"
RESULTS_FILE = "Maverick2/mv_results.jsonl"
PASS_FILE    = "Maverick2/mv_pass.jsonl"
REVIEW_FILE  = "Maverick2/mv_review.jsonl"
FAIL_FILE    = "Maverick2/mv_fail.jsonl"
ERROR_FILE         = "Maverick2/mv_errors.jsonl"
DISAGREEMENT_FILE  = "Maverick2/mv_disagreement.jsonl"

MODEL            = "openai/gpt-oss-120b"
MAX_RETRIES      = 3
RETRY_DELAY_BASE = 30
CALL_TIMEOUT     = 90
DIALOGUE_RETRY1  = 120
DIALOGUE_RETRY2  = 300
DIALOGUE_CONCURRENCY = 4

# ---------------------------------------------------------------------------
# RATE LİMİTER
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rpm: int):
        self.delay = 60.0 / rpm
        self._lock = asyncio.Lock()
        self._next = 0.0
    async def acquire(self):
        async with self._lock:
            now  = time.monotonic()
            wait = self._next - now
            if wait > 0: await asyncio.sleep(wait)
            self._next = time.monotonic() + self.delay

# ---------------------------------------------------------------------------
# PROFİL TANIMLARI (pipeline ile eşleştirildi)
# ---------------------------------------------------------------------------
PROFILE_DESCRIPTIONS = {
    "FAST": (
        "Öğrenci konuya hakimdir, hızlı ilerler, bağlantıları kendisi kurar. "
        "TUTOR HATASI: Boş onay ve tanım tekrarı yapmak. "
        "Mantık sapmasını yakalamadan geçmek. "
        "Temel tanımları açıklamak, derin soru sormamak."
    ),
    "MEM": (
        "Öğrenci tanımı ezberden bilir ama anlamını kavrayamaz, transfer yapamaz. "
        "TUTOR HATASI: Formül ve kuralları sorgulamadan kabul etmek. "
        "'Neden?' sorusu sormadan ilerlemek. "
        "Transfer testi yapmamak, örnek/karşı örnek zorlamamak."
    ),
    "SLOW": (
        "Öğrenci temelde zorlanır, kararsızdır, adım adım ilerlemesi gerekir. "
        "TUTOR HATASI: Çok adım atlamak, acele ettirmek. "
        "Öğrencinin muhakemesini onaylamadan geçmek. "
        "Soyut açıklamalar yapmak, somut örnek vermemek."
    ),
}

FEW_SHOT = """
### Örnek 1 — FAST | Doğru kapanış
[Öğrenci]: Bu dil '100*' düzenli ifadesiyle temsil edilebiliyor.
[Tutor]: Düzenli ifade kullanarak dilin hangi sınıfa girdiğini kendi analizinle belirledin.
S_SON: dogru_kapanis

### Örnek 2 — FAST | Yanlış kapanış
[Öğrenci]: Bu çelişki dilin düzenli olmadığını kanıtlıyor.
[Tutor]: Mükemmel! Başka bir soruya geçebiliriz.
S_SON: yeni_konu_acti → FAIL
"""

CRITIC_PROMPT = """Sen sert bir eğitim denetçisisin. Görevin HATA ve EKSİKLİK bulmak.

{few_shot}

Öğrenci Profili: {profile}
Profil Kuralları: {profile_desc}

## DİYALOG
{messages}

1. YANLIŞ ONAY: Hatalı ifadeyi düzeltmeden onayladı mı?
2. PROFİL UYUMSUZLUĞU: Profile uygun davranmadı mı?
3. TEKNİK HATA: Automata Theory açısından yanlış nokta var mı?
4. TEKRAR: Aynı soru birden fazla kez soruldu mu?
5. SON MESAJ: Yeni konu açıyor mu?

SADECE JSON döndür:
{{"yanlis_onay":[],"profil_uyumsuzlugu":[],"teknik_hata":[],"tekrar":[],"son_mesaj_analizi":"...","genel_not":"..."}}"""

VERDICT_PROMPT = """## KRİTİK ANALİZ
{critic_output}

## DİYALOG
Profil: {profile}
{messages}

Her soru için SADECE belirtilen seçeneklerden birini yaz.

S1. Tutor cevabı doğrudan verdi mi?
[ hiç_vermedi | bazen_verdi | sıklıkla_verdi ]

S2. Soruları doğru yönde yönlendirebildi mi?
[ etkili | kısmen_etkili | etkisiz ]

S3. İpucu dozajı profile uygun muydu?
[ dengeli | fazla_ipucu | az_ipucu ]

S4. Kural/tanımı doğrudan söyledi mi?
[ hayır | evet_ama_gerekli | evet_ve_gereksiz ]

S4a. Söylediyse öğrenci uygulayabildi mi?
[ evet | hayır | fırsat_olmadı | geçerli_değil ]

S5. Öğrenci her turda ilerledi mi?
[ sürekli_ilerledi | çoğunlukla_ilerledi | takılı_kaldı ]

S6. Öğrenci sonuca kendi ulaştı mı?
[ kendisi_ulaştı | tutor_yönlendirdi | tutor_söyledi ]

S7. Tutor davranışı profile uygun muydu?
[ tam_uyumlu | kısmen_uyumlu | uyumsuz ]

S7a. Profil sapması (yoksa "yok"):

S8. Teknik hata var mı?
[ hata_yok | küçük_belirsizlik | ciddi_hata ]

S8_aciklama. Teknik değerlendirme (ZORUNLU):

S9. Benzer soruları tekrar etti mi?
[ hayır | bir_kez_tekrar | sık_tekrar ]

S9a. Tekrar örneği (yoksa "yok"):

S10. Türkçe kalitesi:
[ doğal | kabul_edilebilir | yapay ]

S11. Gerçekten öğretici miydi?
[ evet | kısmen | hayır ]

S11_gerekce. Gerekçe (ZORUNLU):

S_SON. Son mesaj kapanışı:
[ dogru_kapanis | yeni_konu_acti ]

VERDICT KURALLARI:
- fail: S1∈(bazen_verdi,sıklıkla_verdi) VEYA S8=ciddi_hata VEYA S11=hayır
        VEYA S9∈(bir_kez_tekrar,sık_tekrar) VEYA S_SON=yeni_konu_acti
- pass: S1=hiç_vermedi VE S5≠takılı_kaldı VE S8=hata_yok
        VE S11=evet VE S9=hayır VE S_SON=dogru_kapanis
- review: diğer tüm durumlar

verdict: pass | review | fail
red_nedenleri: tetikleyici S değerleri listesi (pass ise [])
gerekce: güçlü yön + zayıf yön (birer cümle)

Confidence. Bu değerlendirmeye ne kadar güveniyorsun?
[ düşük | orta | yüksek ]

Ambiguity. Diyalog yorum açısından ne kadar belirsiz?
[ düşük | orta | yüksek ]

SADECE JSON:
{{"S1":"","S2":"","S3":"","S4":"","S4a":"","S5":"","S6":"","S7":"","S7a":"","S8":"","S8_aciklama":"","S9":"","S9a":"","S10":"","S11":"","S11_gerekce":"","S_SON":"","verdict":"","red_nedenleri":[],"gerekce":"","confidence":"","ambiguity":""}}"""

# ---------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ---------------------------------------------------------------------------
def format_messages(messages):
    rows = []
    for m in (messages or []):
        if not isinstance(m, dict):
            rows.append(f"[Unknown]: {str(m)}")
            continue
        role    = m.get("role", "")
        content = m.get("content", "")
        speaker = "Öğrenci" if role == "user" else ("Tutor" if role == "assistant" else "Unknown")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)
        rows.append(f"[{speaker}]: {content}")
    return "\n\n".join(rows)

def clean_json(raw: str) -> str:
    raw = raw.strip()
    # Backtick bloğu
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                raw = part
                break
    # Stack ile doğru JSON sınırını bul
    start = raw.find("{")
    if start == -1:
        return raw
    stack = []
    in_string = False
    escape = False
    for i, c in enumerate(raw[start:], start):
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{": 
            stack.append(c)
        elif c == "}": 
            stack.pop()
            if not stack:
                return raw[start:i+1]
    return raw

def recalculate_verdict(scores: dict, profile: str = "") -> str:
    s1    = scores.get("S1","")
    s5    = scores.get("S5","")
    s8    = scores.get("S8","")
    s9    = scores.get("S9","")
    s11   = scores.get("S11","")
    s_son = scores.get("S_SON","")

    # S9 tekrar kuralı: SLOW profilde bir_kez_tekrar → review (fail değil)
    s9_fail = s9 == "sık_tekrar" or (s9 == "bir_kez_tekrar" and profile != "SLOW")

    if (s1 in ("bazen_verdi","sıklıkla_verdi") or s8 == "ciddi_hata" or
        s11 == "hayır" or s9_fail or s_son == "yeni_konu_acti"):
        return "fail"
    if (s1 == "hiç_vermedi" and s5 != "takılı_kaldı" and s8 == "hata_yok" and
        s11 == "evet" and s9 == "hayır" and s_son == "dogru_kapanis"):
        return "pass"
    return "review"

VALID = {
    "S1":    {"hiç_vermedi","bazen_verdi","sıklıkla_verdi"},
    "S2":    {"etkili","kısmen_etkili","etkisiz"},
    "S3":    {"dengeli","fazla_ipucu","az_ipucu"},
    "S4":    {"hayır","evet_ama_gerekli","evet_ve_gereksiz"},
    "S4a":   {"evet","hayır","fırsat_olmadı","geçerli_değil"},
    "S5":    {"sürekli_ilerledi","çoğunlukla_ilerledi","takılı_kaldı"},
    "S6":    {"kendisi_ulaştı","tutor_yönlendirdi","tutor_söyledi"},
    "S7":    {"tam_uyumlu","kısmen_uyumlu","uyumsuz"},
    "S8":    {"hata_yok","küçük_belirsizlik","ciddi_hata"},
    "S9":    {"hayır","bir_kez_tekrar","sık_tekrar"},
    "S10":   {"doğal","kabul_edilebilir","yapay"},
    "S11":   {"evet","kısmen","hayır"},
    "S_SON": {"dogru_kapanis","yeni_konu_acti"},
}
VALID_OPTIONAL = {
    "confidence": {"düşük","orta","yüksek"},
    "ambiguity":  {"düşük","orta","yüksek"},
}
REQUIRED_TEXT = {"S8_aciklama","S11_gerekce","gerekce"}

def validate_scores(scores: dict) -> tuple:
    errors = []
    for field, valid_set in VALID.items():
        val = scores.get(field, "")
        if val not in valid_set:
            errors.append(f"{field}='{val}' (geçerli: {valid_set})")
    for field in REQUIRED_TEXT:
        if not scores.get(field, "").strip():
            errors.append(f"{field} boş")
    if not isinstance(scores.get("red_nedenleri"), list):
        errors.append("red_nedenleri liste değil")

    # Semantic validation — alan çelişkileri
    if scores.get("S4") == "hayır" and scores.get("S4a") not in ("geçerli_değil", "fırsat_olmadı"):
        errors.append("S4=hayır ise S4a geçerli_değil veya fırsat_olmadı olmalı")
    if scores.get("S9") == "hayır" and scores.get("S9a","").strip() not in ("yok", ""):
        errors.append("S9=hayır ise S9a yok olmalı")
    # Warning only — "ciddi" içermesi false positive üretebilir
    if scores.get("S8") == "hata_yok" and "ciddi hata" in scores.get("S8_aciklama","").lower():
        print(f"  ⚠️ UYARI: S8=hata_yok ama açıklamada 'ciddi hata' geçiyor")
    if scores.get("S7") == "tam_uyumlu" and scores.get("S7a","").strip() not in ("yok", ""):
        errors.append("S7=tam_uyumlu ise S7a yok olmalı")

    # Optional alanlar — boşsa geçer, doluysa kontrol et
    for field, valid_set in VALID_OPTIONAL.items():
        val = scores.get(field, "")
        if val and val not in valid_set:
            errors.append(f"{field}='{val}' (geçerli: {valid_set})")

    return len(errors) == 0, errors

LEGACY_RESULTS_FILE = "Maverick2/mv_results.jsonl"

def load_done() -> set:
    done = set()
    for path in [RESULTS_FILE, LEGACY_RESULTS_FILE]:
        if not Path(path).exists():
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                try: done.add(json.loads(line)["_key"])
                except: pass
    return done

# ---------------------------------------------------------------------------
# API ÇAĞRISI
# ---------------------------------------------------------------------------
async def api_call(client, messages, temperature, max_tokens, limiter):
    temps = [temperature, max(0.1, temperature - 0.2), 0.1]
    for attempt in range(MAX_RETRIES):
        t = temps[min(attempt, 2)]
        try:
            await limiter.acquire()
            coro = client.chat.completions.create(
                model=MODEL, temperature=t,
                max_tokens=max_tokens,
                messages=messages)
            resp = await asyncio.wait_for(coro, timeout=CALL_TIMEOUT)
            return resp.choices[0].message.content
        except asyncio.TimeoutError:
            print(f"    ⏱ Timeout — {15*(attempt+1)}s bekleniyor...")
            await asyncio.sleep(15 * (attempt + 1))
        except Exception as e:
            err = str(e).lower()
            if "rate limit" in err or "429" in err:
                wait = RETRY_DELAY_BASE * (2 ** attempt)
                print(f"    🚫 429 — {wait}s bekleniyor...")
                await asyncio.sleep(wait)
            else:
                await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError("API başarısız")

# ---------------------------------------------------------------------------
# TEK DİYALOG
# ---------------------------------------------------------------------------
async def process_record(r, label, client, limiter):
    key     = r.get("_key", "")
    profile = r.get("profile", "")
    topic   = r.get("topic", "")[:35]
    msgs    = format_messages(r.get("messages", []))
    pdesc   = PROFILE_DESCRIPTIONS.get(profile, "")

    def make_error(stage, error):
        return {"_error": True, "stage": stage, "error": str(error),
                "_key": key, "profile": profile, "topic": r.get("topic", "")}

    # CRITIC
    try:
        raw    = await api_call(client,
            [{"role": "user", "content": CRITIC_PROMPT.format(
                few_shot=FEW_SHOT, profile=profile,
                profile_desc=pdesc, messages=msgs)}],
            0.4, 3000, limiter)
        critic = json.loads(clean_json(raw))
    except json.JSONDecodeError as e:
        print(f"{label} ⚠️ critic JSON hatası: {e} — {key}")
        return make_error("critic_parse", e)
    except Exception as e:
        print(f"{label} ⚠️ critic API hatası: {e} — {key}")
        return make_error("critic_api", e)

    # VERDICT
    try:
        raw    = await api_call(client,
            [{"role": "user", "content": VERDICT_PROMPT.format(
                critic_output=json.dumps(critic, ensure_ascii=False, indent=2),
                profile=profile, messages=msgs)}],
            0.2, 3000, limiter)
        scores = json.loads(clean_json(raw))
    except json.JSONDecodeError as e:
        print(f"{label} ⚠️ verdict JSON hatası: {e} — {key}")
        return make_error("verdict_parse", e)
    except Exception as e:
        print(f"{label} ⚠️ verdict API hatası: {e} — {key}")
        return make_error("verdict_api", e)

    ok, errs = validate_scores(scores)
    if not ok:
        print(f"{label} ⚠️ schema/semantic hatası: {errs[:2]} — {key}")
        return make_error("schema_validation", "; ".join(errs))

    llm_v  = scores.get("verdict", "?")
    rule_v = recalculate_verdict(scores, profile)
    is_disagreement = llm_v != rule_v
    if is_disagreement:
        scores["verdict_llm"]      = llm_v
        scores["verdict_override"] = True
    scores["verdict"] = rule_v

    icon = {"pass": "✅", "review": "⚠️", "fail": "❌"}.get(rule_v, "?")
    print(f"{label} {icon} {rule_v.upper():6} | {profile} | {topic}")

    return {
        "_key":          key,
        "_qi":           r.get("_qi"),
        "profile":       profile,
        "topic":         r.get("topic", ""),
        "verdict":       rule_v,
        "scores":        scores,
        "_critic":       critic,
        "_disagreement": is_disagreement,
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main():
    limiter = RateLimiter(rpm=20)
    sem     = asyncio.Semaphore(DIALOGUE_CONCURRENCY)
    lock    = asyncio.Lock()

    client = AsyncOpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://mentorx.ai",
            "X-Title": "MentorX Judge",
        }
    )

    try:
        records = [json.loads(l) for l in open(INPUT_FILE, encoding="utf-8") if l.strip()]
    except FileNotFoundError:
        print(f"❌ INPUT_FILE bulunamadı: {INPUT_FILE}")
        return
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse hatası: {e}")
        return
    done    = load_done()
    todo    = [r for r in records if r.get("_key", "") not in done]

    print(f"{'='*50}\nMaverick Judge\n{'='*50}")
    print(f"Toplam:{len(records)} | Bitti:{len(done)} | Kalan:{len(todo)}")
    print(f"Tahmini: ~{len(todo)/10:.0f} dk\n{'='*50}")

    results, fq = [], []
    t0 = time.monotonic()

    async def run_one(r, idx, total, files, retry=0, prev_errors=None):
        async with sem:
            lbl = f"[{'R'+str(retry) if retry else str(idx+1)+'/'+str(total)}]"
            rec = await process_record(r, lbl, client, limiter)
            async with lock:
                if rec is None or rec.get("_error"):
                    minimal_err = {
                        "stage":      rec.get("stage", "unknown") if rec else "none",
                        "error_type": type(rec.get("error","")).__name__ if rec else "unknown",
                        "attempt":    retry,
                    } if rec else {"stage": "none", "attempt": retry}
                    error_history = (prev_errors or []) + [minimal_err]
                    fq.append({"record": r, "errors": error_history}); return
                line = json.dumps(rec, ensure_ascii=False) + "\n"
                files["all"].write(line); files["all"].flush()
                files[rec["verdict"]].write(line); files[rec["verdict"]].flush()
                if rec.get("_disagreement"):
                    files["disagreement"].write(line); files["disagreement"].flush()
                results.append(rec)
                if len(results) % 25 == 0:
                    el   = time.monotonic() - t0
                    rate = len(results) / (el / 60) if el > 0 else 0
                    eta  = (len(todo) - len(results)) / rate if rate > 0 else 0
                    print(f"\n── {len(results)}/{len(todo)} ({len(results)/len(todo)*100:.0f}%) "
                          f"| {rate:.1f}/dk | ETA:~{eta:.0f}dk ──\n")

    Path("Maverick2").mkdir(exist_ok=True)
    for f in [RESULTS_FILE, PASS_FILE, REVIEW_FILE, FAIL_FILE, ERROR_FILE, DISAGREEMENT_FILE]:
        Path(f).touch(exist_ok=True)

    with open(RESULTS_FILE,      "a", encoding="utf-8") as f_all, \
         open(PASS_FILE,         "a", encoding="utf-8") as f_pass, \
         open(REVIEW_FILE,       "a", encoding="utf-8") as f_review, \
         open(FAIL_FILE,         "a", encoding="utf-8") as f_fail, \
         open(ERROR_FILE,        "a", encoding="utf-8") as f_err, \
         open(DISAGREEMENT_FILE, "a", encoding="utf-8") as f_dis:

        files = {"all": f_all, "pass": f_pass, "review": f_review, "fail": f_fail, "disagreement": f_dis}

        await asyncio.gather(*[
            asyncio.create_task(run_one(r, i, len(todo), files))
            for i, r in enumerate(todo)
        ])

        for retry_n, wait_s in [(1, DIALOGUE_RETRY1), (2, DIALOGUE_RETRY2)]:
            if not fq: break
            print(f"\n⏳ {len(fq)} başarısız → {wait_s}s bekleniyor...")
            await asyncio.sleep(wait_s)
            batch = list(fq); fq.clear()
            await asyncio.gather(*[
                asyncio.create_task(run_one(
                    item["record"] if isinstance(item, dict) and "record" in item else item,
                    i, len(batch), files,
                    retry=retry_n,
                    prev_errors=item.get("errors", []) if isinstance(item, dict) else []
                ))
                for i, item in enumerate(batch)
            ])

        if fq:
            for item in fq:
                r = item["record"] if isinstance(item, dict) and "record" in item else item
                err_history = item.get("errors", []) if isinstance(item, dict) else []
                err_rec = {
                    "_key":     r.get("_key"),
                    "stage":    "max_retry",
                    "_error":   True,
                    "error":    "max retry aşıldı",
                    "profile":  r.get("profile", ""),
                    "topic":    r.get("topic", ""),
                    "_error_history": err_history,
                }
                f_err.write(json.dumps(err_rec, ensure_ascii=False) + "\n"); f_err.flush()

    vc = Counter(r["verdict"] for r in results)
    td = sum(vc.values())
    el = (time.monotonic() - t0) / 60

    # Hata istatistikleri
    error_stages = Counter()
    with open(ERROR_FILE, encoding="utf-8") as ef:
        for line in ef:
            try:
                rec = json.loads(line)
                error_stages[rec.get("stage", "unknown")] += 1
            except:
                pass

    # Override/disagreement sayısı
    override_count     = sum(1 for r in results if r.get("scores", {}).get("verdict_override"))
    disagreement_count = sum(1 for r in results if r.get("_disagreement"))

    print(f"\n{'='*50}\nSONUÇ\n{'='*50}")
    if td:
        print(f"✅ Pass      : {vc.get('pass',0):5}  ({vc.get('pass',0)/td*100:.1f}%)")
        print(f"⚠️  Review    : {vc.get('review',0):5}  ({vc.get('review',0)/td*100:.1f}%)")
        print(f"❌ Fail      : {vc.get('fail',0):5}  ({vc.get('fail',0)/td*100:.1f}%)")
    print(f"🔄 Override  : {override_count}")
    print(f"⚡ Disagree  : {disagreement_count}")

    # Confidence / Ambiguity dağılımı
    conf_fail   = sum(1 for r in results if r.get("scores",{}).get("confidence") == "düşük" and r.get("verdict") == "fail")
    amb_review  = sum(1 for r in results if r.get("scores",{}).get("ambiguity")  == "yüksek" and r.get("verdict") == "review")
    conf_dist   = Counter(r.get("scores",{}).get("confidence","?") for r in results)
    amb_dist    = Counter(r.get("scores",{}).get("ambiguity","?")  for r in results)
    print(f"\n── Confidence Dağılımı ──")
    for k in ["yüksek","orta","düşük","?"]:
        if conf_dist[k]: print(f"  {k}: {conf_dist[k]}")
    print(f"  ⚠️  Düşük confidence + fail: {conf_fail}")
    print(f"\n── Ambiguity Dağılımı ──")
    for k in ["düşük","orta","yüksek","?"]:
        if amb_dist[k]: print(f"  {k}: {amb_dist[k]}")
    print(f"  ⚠️  Yüksek ambiguity + review: {amb_review}")

    if error_stages:
        print(f"\n── Hata Dağılımı ──")
        for stage, count in error_stages.most_common():
            print(f"  {stage}: {count}")
    print(f"\n── Profil Bazlı ──")
    by_p = defaultdict(Counter)
    for r in results: by_p[r["profile"]][r["verdict"]] += 1
    for p in sorted(by_p):
        pv  = by_p[p]
        ptd = sum(pv.values())
        fail_rate = pv.get("fail", 0) / ptd * 100 if ptd else 0
        print(f"  {p}: pass={pv.get('pass',0)} review={pv.get('review',0)} fail={pv.get('fail',0)} (fail %{fail_rate:.0f})")
    print(f"\n⏱ {el:.0f} dk")
    print(f"→ {RESULTS_FILE} | {PASS_FILE} | {REVIEW_FILE} | {FAIL_FILE} | {DISAGREEMENT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())