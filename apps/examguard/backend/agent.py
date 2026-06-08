# ExamGuard - agent.py
# Groq Vision (LLaMA) ile ekran görüntüsü analizi

import os
import re
import threading
import requests

try:
    from groq import Groq
except Exception:
    Groq = None

# ── Config ──
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
MODEL        = 'llama-3.2-11b-vision-preview'
GROQ_API_URL = 'https://api.groq.com/openai/v1/chat/completions'

# Periyodik SS'leri analiz etmek için kuyruk
# (Her SS'i anında göndermek yerine sırayla işle)
MAX_CONCURRENT_ANALYSES = int(os.environ.get('MAX_CONCURRENT_ANALYSES', '3'))
_analysis_slots = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)

# ── Prompt ──
SYSTEM_PROMPT = """Sen bir sınav güvenlik sistemisin. 
Sana öğrenci bilgisayarının ekran görüntüsü gönderilecek.

Görevin: Ekranda sınav dışı bir içerik olup olmadığını tespit et.

ŞÜPHELİ sayılanlar:
- ChatGPT, Claude, Gemini, Copilot gibi AI araçları
- Google Translate, DeepL gibi çeviri araçları
- Sosyal medya (Twitter/X, Instagram, Facebook, YouTube)
- Haber siteleri
- Başka bir sınav veya ödev sayfası
- Kod paylaşım siteleri (GitHub, StackOverflow) — IDE modu değilse
- WhatsApp Web, Telegram gibi mesajlaşma araçları
- Sınav sayfası dışında herhangi bir içerik

NORMAL sayılanlar:
- Moodle sınav sayfası
- Boş sekme veya yükleniyor ekranı
- İzin verilen IDE (kodlama sınavında)

ÇIKTI FORMATI (kesinlikle bu formatta yaz, başka hiçbir şey yazma):
KARAR: ŞÜPHELI veya TEMİZ
GEREKÇE: (1 cümle, Türkçe)
"""

def analyze(screenshot_base64: str, mode: str = 'web', context: dict | None = None) -> dict:
    """
    SS'i VLM ile analiz eder.
    
    Returns:
        {
            'suspicious': bool,
            'verdict': 'ŞÜPHELI' | 'TEMİZ',
            'reason': str,
            'raw': str
        }
    """
    if not GROQ_API_KEY:
        return _fallback_result('Groq API key ayarlanmamış')

    # Önce SDK dene; proxies/httpx uyumsuzluğu gibi durumlarda REST fallback'e düş.
    if Groq is not None:
        try:
            return _analyze_via_sdk(screenshot_base64, mode, context=context)
        except Exception as e:
            err = str(e)
            if "unexpected keyword argument 'proxies'" in err:
                print('[Agent] SDK proxies uyumsuzluğu, REST fallback çalışacak')
            else:
                print(f'[Agent] SDK hatası, REST fallback çalışacak: {e}')

    try:
        raw = _analyze_via_rest(screenshot_base64, mode, context=context)
        return _parse_response(raw)
    except Exception as e:
        print(f'[Agent] Groq hatası: {e}')
        return _fallback_result(str(e))


def _build_mode_note(mode: str, context: dict | None = None) -> str:
    mode_note = ''
    if mode == 'coding':
        mode_note = '\nNot: Bu kodlama sınavıdır. IDE ve dokümantasyon siteleri normaldir.'
    if context:
        ctx_title = context.get('activeWindowTitle') or ''
        ctx_proc = context.get('activeProcess') or ''
        if ctx_title or ctx_proc:
            mode_note += f"\nEk bağlam: aktif pencere='{ctx_title}', süreç='{ctx_proc}'."
    return mode_note


def _extract_b64(screenshot_base64: str) -> str:
    if ',' in screenshot_base64:
        return screenshot_base64.split(',')[1]
    return screenshot_base64


def _analyze_via_sdk(screenshot_base64: str, mode: str, context: dict | None = None) -> dict:
    client = Groq(api_key=GROQ_API_KEY)
    b64_data = _extract_b64(screenshot_base64)
    mode_note = _build_mode_note(mode, context=context)

    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=120,
        messages=[
            {
                'role': 'system',
                'content': SYSTEM_PROMPT + mode_note
            },
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': f'data:image/jpeg;base64,{b64_data}'
                        }
                    },
                    {
                        'type': 'text',
                        'text': 'Bu ekran görüntüsünü analiz et.'
                    }
                ]
            }
        ]
    )
    raw = response.choices[0].message.content.strip()
    return _parse_response(raw)


def _analyze_via_rest(screenshot_base64: str, mode: str, context: dict | None = None) -> str:
    b64_data = _extract_b64(screenshot_base64)
    mode_note = _build_mode_note(mode, context=context)

    payload = {
        'model': MODEL,
        'max_tokens': 120,
        'messages': [
            {
                'role': 'system',
                'content': SYSTEM_PROMPT + mode_note
            },
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': f'data:image/jpeg;base64,{b64_data}'
                        }
                    },
                    {
                        'type': 'text',
                        'text': 'Bu ekran görüntüsünü analiz et.'
                    }
                ]
            }
        ]
    }
    headers = {
        'Authorization': f'Bearer {GROQ_API_KEY}',
        'Content-Type': 'application/json'
    }
    resp = requests.post(GROQ_API_URL, json=payload, headers=headers, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    raw = (((data.get('choices') or [{}])[0]).get('message') or {}).get('content', '')
    return (raw or '').strip()


def analyze_async(screenshot_base64: str, mode: str, callback, context: dict | None = None):
    """
    SS analizini ayrı thread'de çalıştırır.
    Sonuç hazır olunca callback(result) çağrılır.
    """
    def run():
        acquired = _analysis_slots.acquire(timeout=2)
        if not acquired:
            callback(_fallback_result('Analiz kuyruğu dolu'))
            return
        try:
            result = analyze(screenshot_base64, mode, context=context)
            callback(result)
        finally:
            _analysis_slots.release()

    t = threading.Thread(target=run, daemon=True)
    t.start()


def _parse_response(raw: str) -> dict:
    """VLM çıktısını parse eder."""
    verdict   = 'BELİRSİZ'
    reason    = raw or 'Model geçerli bir karar döndürmedi.'
    suspicious = False

    # KARAR satırını bul
    karar_match = re.search(r'KARAR:\s*(ŞÜPHELİ|TEMİZ|ŞÜPHELI)', raw, re.IGNORECASE)
    if karar_match:
        verdict_raw = karar_match.group(1).upper()
        if 'PHE' in verdict_raw:  # ŞÜPHELI veya ŞÜPHELİ
            verdict    = 'ŞÜPHELİ'
            suspicious = True
        else:
            verdict = 'TEMİZ'

    # GEREKÇE satırını bul
    gerekce_match = re.search(r'GEREKÇE:\s*(.+)', raw, re.IGNORECASE)
    if gerekce_match:
        reason = gerekce_match.group(1).strip()

    if not karar_match:
        return {
            'suspicious': False,
            'verdict': 'HATA',
            'reason': reason,
            'raw': raw
        }

    return {
        'suspicious': suspicious,
        'verdict':    verdict,
        'reason':     reason,
        'raw':        raw
    }


def _fallback_result(error_msg: str) -> dict:
    if "unexpected keyword argument 'proxies'" in (error_msg or ''):
        error_msg = (
            "httpx/groq sürüm uyumsuzluğu (cozum: httpx==0.27.2 ve bagimliliklari yeniden kur)"
        )
    return {
        'suspicious': False,
        'verdict':    'HATA',
        'reason':     f'Analiz yapılamadı: {error_msg}',
        'raw':        ''
    }
