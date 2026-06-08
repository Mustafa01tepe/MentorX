"""
judge_deepseek.py
─────────────────
Sadece DeepSeek V3 ile değerlendirme yapar.
Her diyalog için: critic → verdict → dosyaya yaz

Çıktılar:
  ds_results.jsonl   → tüm sonuçlar
  ds_pass.jsonl      → pass
  ds_review.jsonl    → review
  ds_fail.jsonl      → fail
  ds_errors.jsonl    → pipeline hataları
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
INPUT_FILE   = "dialogs/passed_clean.jsonl"
RESULTS_FILE = "DeepSeek/ds_results.jsonl"
PASS_FILE    = "DeepSeek/ds_pass.jsonl"
REVIEW_FILE  = "DeepSeek/ds_review.jsonl"
FAIL_FILE    = "DeepSeek/ds_fail.jsonl"
ERROR_FILE   = "DeepSeek/ds_errors.jsonl"

MODEL            = "deepseek-chat"
MAX_RETRIES      = 3
RETRY_DELAY_BASE = 30   # 429 için üstel backoff (sn)
CALL_TIMEOUT     = 90   # tek çağrı max bekleme (sn)
DIALOGUE_RETRY1  = 120
DIALOGUE_RETRY2  = 300
DIALOGUE_CONCURRENCY = 6

# ---------------------------------------------------------------------------
# RATE LİMİTER  (DeepSeek: dinamik, 20 RPM temkinli)
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
# PROFİL TANIMLARI
# ---------------------------------------------------------------------------
PROFILE_DESCRIPTIONS = {
    "FAST": (
        "Temel kavramları zaten biliyor, hızlı ilerler. "
        "TUTOR HATASI: temel tanımı açıklamak, 'Harika!' gibi boş onaylar vermek."
    ),
    "SLOW": (
        "Temel konularda zorlanır, kararsızdır. Küçük adımlarla ilerlemesi gerekir. "
        "TUTOR HATASI: Çok adım atlamak, öğrencinin yanlış muhakemesini onaylamak."
    ),
    "MEM": (
        "Tanımı ezberden bilir ama anlamını kavrayamaz, transfer yapamaz. "
        "TUTOR HATASI: 'neden?' sorusu sormadan ilerlemek, transfer testi yapmamak."
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

SADECE JSON:
{{"S1":"","S2":"","S3":"","S4":"","S4a":"","S5":"","S6":"","S7":"","S7a":"","S8":"","S8_aciklama":"","S9":"","S9a":"","S10":"","S11":"","S11_gerekce":"","S_SON":"","verdict":"","red_nedenleri":[],"gerekce":""}}"""

# ---------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ---------------------------------------------------------------------------
def format_messages(messages):
    return "\n\n".join(
        f"[{'Öğrenci' if m['role']=='user' else 'Tutor'}]: {m['content']}"
        for m in messages
    )

def clean_json(raw: str) -> str:
    raw = raw.strip()
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        return raw[start:end+1]
    return raw

def recalculate_verdict(scores: dict) -> str:
    s1    = scores.get("S1","")
    s5    = scores.get("S5","")
    s8    = scores.get("S8","")
    s9    = scores.get("S9","")
    s11   = scores.get("S11","")
    s_son = scores.get("S_SON","")
    if (s1 in ("bazen_verdi","sıklıkla_verdi") or s8 == "ciddi_hata" or
        s11 == "hayır" or s9 in ("bir_kez_tekrar","sık_tekrar") or
        s_son == "yeni_konu_acti"):
        return "fail"
    if (s1 == "hiç_vermedi" and s5 != "takılı_kaldı" and s8 == "hata_yok" and
        s11 == "evet" and s9 == "hayır" and s_son == "dogru_kapanis"):
        return "pass"
    return "review"

# Geçerli değer listeleri
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
    "verdict": {"pass","review","fail"},
}
REQUIRED_TEXT = {"S8_aciklama","S11_gerekce","gerekce"}

def validate_scores(scores: dict) -> tuple:
    """
    Tüm S alanlarının geçerli değer taşıdığını kontrol eder.
    Hatalı alan varsa (field, value) listesi döner.
    """
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
    return len(errors) == 0, errors

def load_done() -> set:
    done = set()
    if Path(RESULTS_FILE).exists():
        with open(RESULTS_FILE, encoding="utf-8") as f:
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

    # CRITIC
    try:
        raw    = await api_call(client,
            [{"role": "user", "content": CRITIC_PROMPT.format(
                few_shot=FEW_SHOT, profile=profile,
                profile_desc=pdesc, messages=msgs)}],
            0.4, 3000, limiter)
        critic = json.loads(clean_json(raw))
    except Exception as e:
        print(f"{label} ⚠️ critic hata: {e} — {key}")
        return None

    # VERDICT
    try:
        raw    = await api_call(client,
            [{"role": "user", "content": VERDICT_PROMPT.format(
                critic_output=json.dumps(critic, ensure_ascii=False, indent=2),
                profile=profile, messages=msgs)}],
            0.2, 3000, limiter)
        scores = json.loads(clean_json(raw))
    except Exception as e:
        print(f"{label} ⚠️ verdict hata: {e} — {key}")
        return None

    # Doğrulama
    ok, errs = validate_scores(scores)
    if not ok:
        print(f"{label} ⚠️ geçersiz alan(lar): {errs[:2]} — {key}")
        return None

    llm_v  = scores.get("verdict", "?")
    rule_v = recalculate_verdict(scores)
    if llm_v != rule_v:
        scores["verdict_llm"]      = llm_v
        scores["verdict_override"] = True
    scores["verdict"] = rule_v

    icon = {"pass": "✅", "review": "⚠️", "fail": "❌"}.get(rule_v, "?")
    print(f"{label} {icon} {rule_v.upper():6} | {profile} | {topic}")

    return {
        "_key":    key,
        "_qi":     r.get("_qi"),
        "profile": profile,
        "topic":   r.get("topic", ""),
        "verdict": rule_v,
        "scores":  scores,
        "_critic": critic,
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main():
    limiter = RateLimiter(rpm=20)
    sem     = asyncio.Semaphore(DIALOGUE_CONCURRENCY)
    lock    = asyncio.Lock()

    client = AsyncOpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )

    records = [json.loads(l) for l in open(INPUT_FILE, encoding="utf-8") if l.strip()]
    done    = load_done()
    todo    = [r for r in records if r.get("_key", "") not in done]

    print(f"{'='*50}\nDeepSeek Judge\n{'='*50}")
    print(f"Toplam:{len(records)} | Bitti:{len(done)} | Kalan:{len(todo)}")
    print(f"Tahmini: ~{len(todo)/10:.0f} dk\n{'='*50}")

    results, fq = [], []
    t0 = time.monotonic()

    async def run_one(r, idx, total, files, retry=0):
        async with sem:
            lbl = f"[{'R'+str(retry) if retry else str(idx+1)+'/'+str(total)}]"
            rec = await process_record(r, lbl, client, limiter)
            async with lock:
                if rec is None:
                    fq.append(r); return
                line = json.dumps(rec, ensure_ascii=False) + "\n"
                files["all"].write(line); files["all"].flush()
                files[rec["verdict"]].write(line); files[rec["verdict"]].flush()
                results.append(rec)
                if len(results) % 25 == 0:
                    el   = time.monotonic() - t0
                    rate = len(results) / (el / 60) if el > 0 else 0
                    eta  = (len(todo) - len(results)) / rate if rate > 0 else 0
                    print(f"\n── {len(results)}/{len(todo)} ({len(results)/len(todo)*100:.0f}%) "
                          f"| {rate:.1f}/dk | ETA:~{eta:.0f}dk ──\n")

    with open(RESULTS_FILE, "a", encoding="utf-8") as f_all, \
         open(PASS_FILE,    "a", encoding="utf-8") as f_pass, \
         open(REVIEW_FILE,  "a", encoding="utf-8") as f_review, \
         open(FAIL_FILE,    "a", encoding="utf-8") as f_fail, \
         open(ERROR_FILE,   "a", encoding="utf-8") as f_err:

        files = {"all": f_all, "pass": f_pass, "review": f_review, "fail": f_fail}

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
                asyncio.create_task(run_one(r, i, len(batch), files, retry=retry_n))
                for i, r in enumerate(batch)
            ])

        if fq:
            for r in fq:
                f_err.write(json.dumps({"_key": r.get("_key"), "_error": "max_retry"}, ensure_ascii=False) + "\n")

    vc = Counter(r["verdict"] for r in results)
    td = sum(vc.values())
    el = (time.monotonic() - t0) / 60
    print(f"\n{'='*50}\nSONUÇ\n{'='*50}")
    if td:
        print(f"✅ Pass  : {vc.get('pass',0):5}  ({vc.get('pass',0)/td*100:.1f}%)")
        print(f"⚠️  Review: {vc.get('review',0):5}  ({vc.get('review',0)/td*100:.1f}%)")
        print(f"❌ Fail  : {vc.get('fail',0):5}  ({vc.get('fail',0)/td*100:.1f}%)")
    print(f"⏱ {el:.0f} dk")
    by_p = defaultdict(Counter)
    for r in results: by_p[r["profile"]][r["verdict"]] += 1
    for p in sorted(by_p):
        pv = by_p[p]; print(f"  {p}: pass={pv.get('pass',0)} review={pv.get('review',0)} fail={pv.get('fail',0)}")
    print(f"\n→ {RESULTS_FILE} | {PASS_FILE} | {REVIEW_FILE} | {FAIL_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
