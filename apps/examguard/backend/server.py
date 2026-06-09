# ExamGuard - server.py
from dotenv import load_dotenv
load_dotenv()
# monkey_patch her şeyden önce gelmeli
import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, join_room
from flask_cors import CORS
import base64, binascii, os, re, secrets, threading, uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from agent import analyze_async
from policy import record_coding_vote, resolve_student_session
from state_store import create_state_store

app = Flask(__name__, static_folder='dashboard')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_urlsafe(32)
CORS(app, resources={r"/*": {"origins": [
    "http://localhost:5000",
    "http://127.0.0.1:5000",
    re.compile(r"^chrome-extension://[a-z]{32}$")
]}})
socketio = SocketIO(
    app,
    cors_allowed_origins=['http://localhost:5000', 'http://127.0.0.1:5000'],
    async_mode='eventlet'
)

SCREENSHOTS_DIR = os.environ.get('SCREENSHOTS_DIR', '/tmp/examguard_screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
CONFIGURED_ADMIN_TOKEN = (os.environ.get('ADMIN_TOKEN') or '').strip()
ADMIN_TOKEN = CONFIGURED_ADMIN_TOKEN or secrets.token_urlsafe(24)
if not CONFIGURED_ADMIN_TOKEN:
    print(f'[ExamGuard] ADMIN_TOKEN tanımlı değil. Geçici öğretmen tokenı: {ADMIN_TOKEN}')
else:
    print(f'[ExamGuard] ADMIN_TOKEN yüklendi (uzunluk: {len(ADMIN_TOKEN)})')
MAX_SCREENSHOT_BYTES = int(os.environ.get('MAX_SCREENSHOT_BYTES', 5 * 1024 * 1024))
MAX_REQUEST_BYTES = int(os.environ.get('MAX_REQUEST_BYTES', 8 * 1024 * 1024))
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_BYTES

# ── Sınav durumu ──
exam_state = {
    'active':       False,
    'mode':         'web',
    'duration':     90,
    'started_at':   None,
    'allowed_urls': [],
    'exam_code':    ''    # hoca dashboard'dan belirler
}

# ── Öğrenci kayıtları ──
students = {}
student_sessions = {}
admin_sockets = set()
verify_attempts = defaultdict(deque)
analysis_registry = {}
analysis_registry_lock = threading.Lock()
coding_vlm_votes = defaultdict(lambda: deque(maxlen=3))

STATE_DB_PATH = os.environ.get(
    'STATE_DB_PATH',
    os.path.join(os.path.dirname(SCREENSHOTS_DIR), 'examguard_state.sqlite3')
)
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
state_store = create_state_store(
    database_url=DATABASE_URL,
    sqlite_path=STATE_DB_PATH,
    connect_attempts=os.environ.get('DATABASE_CONNECT_ATTEMPTS', '8'),
    retry_delay=os.environ.get('DATABASE_RETRY_DELAY_SECONDS', '2')
)
print(
    '[ExamGuard] Durum deposu: '
    + ('PostgreSQL' if DATABASE_URL else f'SQLite ({STATE_DB_PATH})')
)
persisted_state = state_store.load()
exam_state.update(persisted_state.get('exam_state') or {})
students.update(persisted_state.get('students') or {})
student_sessions.update(persisted_state.get('student_sessions') or {})

ANALYSIS_TIMEOUT_SECONDS = 20

DIRECT_SUSPICIOUS_REASONS = {
    'tab_switch',
    'new_tab_attempt',
    'ai_extension_detected',
    'resource_access',
}

FORCE_VLM_REASONS = {
    'window_unfocused',
    'desktop_unfocused',
    'periodic',
    'desktop_periodic',
}

CODING_ALLOW_PROCESS = {
    'code.exe', 'pycharm64.exe', 'idea64.exe', 'rider64.exe',
    'webstorm64.exe', 'devenv.exe', 'sublime_text.exe',
    'notepad++.exe', 'cursor.exe', 'chrome.exe', 'msedge.exe',
    'firefox.exe', 'cmd.exe', 'powershell.exe', 'windows terminal.exe',
    'wt.exe', 'explorer.exe'
}

CODING_HARD_BLOCK_KEYWORDS = (
    'chatgpt', 'chat.openai.com', 'claude', 'gemini', 'perplexity',
    'deepseek', 'copilot chat', 'whatsapp', 'telegram',
    'instagram', 'twitter', 'x.com', 'facebook', 'youtube'
)

CODING_MEDIUM_RISK_KEYWORDS = (
    'stackoverflow', 'stack overflow', 'chegg', 'coursehero',
    'brainly', 'reddit', 'quora'
)

EVENT_LABELS = {
    'tab_switch':            '⚠️ Sekme Değişimi',
    'window_unfocused':      '⚠️ Chrome Arka Plana',
    'new_tab_attempt':       '🚨 Yeni Sekme Teşebbüsü',
    'ai_extension_detected': '🚨 AI Eklentisi',
    'resource_access':       '📄 Kaynak Erişimi',
    'periodic':              '📸 Periyodik',
    'desktop_unfocused':     '🖥️ Masaüstü Geçişi',
    'desktop_periodic':      '🖥️ Masaüstü Periyodik',
}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def persist_state():
    state_store.save(exam_state, students, student_sessions)

def public_exam_state():
    return {k: v for k, v in exam_state.items() if k != 'exam_code'}

def get_bearer_token():
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return ''

def authenticated_student(data):
    token = get_bearer_token() or (data or {}).get('sessionToken', '')
    claimed_sid = ((data or {}).get('student') or {}).get('id', '')
    return resolve_student_session(token, claimed_sid, student_sessions)

def require_admin_socket():
    if request.sid not in admin_sockets:
        socketio.emit(
            'admin_error',
            {'message': 'Bu işlem için öğretmen yetkisi gerekiyor.'},
            to=request.sid
        )
        return False
    return True

def safe_filename_part(value, fallback):
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value or ''))[:80]
    return cleaned or fallback

def emit_students():
    socketio.emit('students_update', list(students.values()), to='admins')

def increase_alert_count(sid: str):
    if sid in students:
        students[sid]['alertCount'] = students[sid].get('alertCount', 0) + 1
        persist_state()
        emit_students()

def get_text_haystack(*parts):
    return ' '.join([(p or '') for p in parts]).lower()

def assess_coding_risk(reason, tab_url, tab_title, client_ctx):
    process_name = (client_ctx.get('activeProcess') or '').lower()
    win_title = client_ctx.get('activeWindowTitle') or ''
    haystack = get_text_haystack(tab_url, tab_title, win_title, process_name)

    hard_hits = [k for k in CODING_HARD_BLOCK_KEYWORDS if k in haystack]
    if hard_hits:
        return {
            'route': 'direct_alert',
            'risk': 'high',
            'note': f"Kodlama modunda yasak içerik izi: {hard_hits[0]}"
        }

    medium_hits = [k for k in CODING_MEDIUM_RISK_KEYWORDS if k in haystack]
    if medium_hits:
        return {
            'route': 'require_vlm',
            'risk': 'medium',
            'note': f"Kodlama modunda şüpheli kaynak izi: {medium_hits[0]}"
        }

    if process_name and process_name not in CODING_ALLOW_PROCESS:
        return {
            'route': 'require_vlm',
            'risk': 'medium',
            'note': f"Kodlama modunda allowlist dışı uygulama: {process_name}"
        }

    if reason in ('desktop_periodic', 'periodic', 'desktop_unfocused', 'window_unfocused'):
        return {
            'route': 'require_vlm',
            'risk': 'low',
            'note': 'Kodlama modunda düzenli VLM doğrulaması'
        }

    return {
        'route': 'direct_clean',
        'risk': 'low',
        'note': 'Kodlama modunda temiz bağlam'
    }

def decide_routing(reason, mode, tab_url, tab_title, client_ctx):
    if reason in DIRECT_SUSPICIOUS_REASONS:
        return {'route': 'direct_alert', 'risk': 'high', 'note': 'Kural tabanlı net ihlal'}

    if mode == 'coding':
        return assess_coding_risk(reason, tab_url, tab_title, client_ctx)

    if reason in FORCE_VLM_REASONS:
        return {'route': 'require_vlm', 'risk': 'medium', 'note': 'Web modunda VLM zorunlu olay'}

    return {'route': 'require_vlm', 'risk': 'low', 'note': 'Varsayılan VLM doğrulaması'}

def start_analysis_timeout(event_id, payload, sid):
    def on_timeout():
        with analysis_registry_lock:
            state = analysis_registry.get(event_id)
            if not state or state.get('done'):
                return
            state['done'] = True
            analysis_registry.pop(event_id, None)

        timeout_payload = {
            **payload,
            'suspicious': False,
            'agentVerdict': 'BELİRSİZ',
            'agentReason': 'VLM yanıt süresi aşıldı, manuel inceleme önerilir.',
            'analysisStatus': 'uncertain'
        }
        socketio.emit('screenshot', timeout_payload, to='admins')

    eventlet.spawn_after(ANALYSIS_TIMEOUT_SECONDS, on_timeout)

# ─────────────────────────────────────────
# ÖĞRENCİ ENDPOINT'LERİ
# ─────────────────────────────────────────
@app.route('/student/join', methods=['POST'])
def student_join():
    data    = request.json or {}
    sid = authenticated_student(data)
    if not sid:
        return jsonify({'success': False, 'message': 'Geçersiz öğrenci oturumu.'}), 401
    student = students.get(sid, {})
    students[sid] = {
        'id':          sid,
        'name':        student.get('name', 'Bilinmiyor'),
        'connectedAt': data.get('timestamp', now_iso()),
        'lastSeen':    data.get('timestamp', now_iso()),
        'alertCount':  students.get(sid, {}).get('alertCount', 0),
        'status':      'active'
    }
    persist_state()
    emit_students()
    print(f"[+] {student.get('name')} bağlandı")
    return jsonify({'success': True})

@app.route('/student/leave', methods=['POST'])
def student_leave():
    data = request.json or {}
    sid = authenticated_student(data)
    if not sid:
        return jsonify({'success': False, 'message': 'Geçersiz öğrenci oturumu.'}), 401
    if sid in students:
        students[sid]['status']   = 'left'
        students[sid]['lastSeen'] = data.get('timestamp', now_iso())
        persist_state()
    emit_students()
    return jsonify({'success': True})

@app.route('/student/heartbeat', methods=['POST'])
def student_heartbeat():
    data = request.json or {}
    sid = authenticated_student(data)
    if not sid:
        return jsonify({'success': False, 'message': 'Geçersiz öğrenci oturumu.'}), 401
    if sid in students:
        students[sid]['lastSeen'] = data.get('timestamp', now_iso())
        students[sid]['status']   = 'active'
        persist_state()
        emit_students()
    return jsonify({'success': True})

# ─────────────────────────────────────────
# ÖĞRENCİ DOĞRULAMA
# ─────────────────────────────────────────
@app.route('/student/verify', methods=['POST'])
def student_verify():
    data = request.json or {}
    code = data.get('code', '').strip()
    name = data.get('name', '').strip()
    sid  = data.get('id', '').strip()

    if not name or not sid or len(name) > 100 or len(sid) > 64:
        return jsonify({'success': False, 'message': 'Öğrenci bilgileri geçersiz.'}), 400

    if not exam_state.get('active'):
        return jsonify({'success': False, 'message': 'Aktif sınav bulunamadı.'})

    if not exam_state.get('exam_code'):
        return jsonify({'success': False, 'message': 'Sınav kodu belirlenmemiş.'})

    attempt_key = request.remote_addr or 'unknown'
    attempts = verify_attempts[attempt_key]
    cutoff = datetime.now(timezone.utc).timestamp() - 60
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if len(attempts) >= 10:
        return jsonify({
            'success': False,
            'message': 'Çok fazla deneme yapıldı. Bir dakika sonra tekrar deneyin.'
        }), 429

    if not secrets.compare_digest(code, exam_state['exam_code']):
        attempts.append(datetime.now(timezone.utc).timestamp())
        return jsonify({'success': False, 'message': 'Sınav kodu hatalı.'})

    # Doğrulama başarılı — öğrenciyi kaydet
    attempts.clear()
    session_token = secrets.token_urlsafe(32)
    student_sessions[session_token] = sid
    students[sid] = {
        'id':          sid,
        'name':        name,
        'connectedAt': now_iso(),
        'lastSeen':    now_iso(),
        'alertCount':  students.get(sid, {}).get('alertCount', 0),
        'status':      'active'
    }
    persist_state()
    emit_students()
    print(f'[+] Doğrulandı: {name} ({sid})')
    return jsonify({'success': True, 'sessionToken': session_token})


# ─────────────────────────────────────────
# EKRAN GÖRÜNTÜSÜ
# ─────────────────────────────────────────
@app.route('/screenshot', methods=['POST'])
def receive_screenshot():
    data       = request.json or {}
    authenticated_sid = authenticated_student(data)
    if not authenticated_sid:
        return jsonify({'success': False, 'message': 'Geçersiz öğrenci oturumu.'}), 401
    reason     = data.get('reason', 'periodic')
    student    = data.get('student') or {}
    timestamp  = data.get('timestamp', now_iso())
    screenshot = data.get('screenshot', '')
    tab_url    = data.get('tabUrl', '')
    tab_title  = data.get('tabTitle', '')
    sid        = authenticated_sid
    student['id'] = sid
    mode       = exam_state.get('mode', 'web')
    client_ctx = data.get('clientContext') or {}
    event_id   = data.get('eventId') or str(uuid.uuid4())

    if sid in students:
        students[sid]['lastSeen'] = timestamp
        students[sid]['status']   = 'active'
        persist_state()

    safe_ts = safe_filename_part(timestamp, now_iso().replace(':', '-'))
    safe_sid = safe_filename_part(sid, 'unknown')
    safe_reason = safe_filename_part(reason, 'periodic')
    filename = f"{safe_sid}_{safe_reason}_{safe_ts}.jpg"
    filepath = os.path.join(SCREENSHOTS_DIR, filename)
    allowed_prefixes = (
        'data:image/jpeg;base64,',
        'data:image/jpg;base64,',
        'data:image/png;base64,',
    )
    if screenshot.startswith(allowed_prefixes):
        try:
            img_data = base64.b64decode(screenshot.split(',', 1)[1], validate=True)
        except (ValueError, binascii.Error, IndexError):
            return jsonify({'success': False, 'message': 'Geçersiz görüntü verisi.'}), 400
        if len(img_data) > MAX_SCREENSHOT_BYTES:
            return jsonify({'success': False, 'message': 'Görüntü boyutu sınırı aşıldı.'}), 413
        is_jpeg = img_data.startswith(b'\xff\xd8\xff')
        is_png = img_data.startswith(b'\x89PNG\r\n\x1a\n')
        if not (is_jpeg or is_png):
            return jsonify({'success': False, 'message': 'Desteklenmeyen görüntü biçimi.'}), 400
        with open(filepath, 'wb') as f:
            f.write(img_data)
    else:
        return jsonify({'success': False, 'message': 'Ekran görüntüsü zorunludur.'}), 400

    routing = decide_routing(reason, mode, tab_url, tab_title, client_ctx)

    base_payload = {
        'eventId':     event_id,
        'student':     student,
        'reason':      reason,
        'reasonLabel': EVENT_LABELS.get(reason, '📸 Periyodik'),
        'timestamp':   timestamp,
        'tabUrl':      tab_url,
        'tabTitle':    tab_title,
        'screenshot':  screenshot,
        'filename':    filename,
        'mode':        mode,
        'clientContext': client_ctx,
        'routingNote': routing.get('note')
    }

    route = routing.get('route')
    if route == 'direct_alert':
        increase_alert_count(sid)
        payload = {
            **base_payload,
            'suspicious': True,
            'agentVerdict': 'ŞÜPHELİ',
            'agentReason': routing.get('note'),
            'analysisStatus': 'suspicious'
        }
        socketio.emit('alert', payload, to='admins')
        return jsonify({'success': True})

    if route == 'direct_clean':
        payload = {
            **base_payload,
            'suspicious': False,
            'agentVerdict': 'TEMİZ',
            'agentReason': routing.get('note'),
            'analysisStatus': 'clean'
        }
        socketio.emit('screenshot', payload, to='admins')
        return jsonify({'success': True})

    pending_payload = {
        **base_payload,
        'suspicious': None,
        'agentVerdict': None,
        'agentReason': 'Analiz bekleniyor...',
        'analysisStatus': 'pending'
    }
    socketio.emit('analysis_pending', pending_payload, to='admins')

    with analysis_registry_lock:
        analysis_registry[event_id] = {'done': False}

    start_analysis_timeout(event_id, base_payload, sid)

    def on_agent_done(result):
        with analysis_registry_lock:
            state = analysis_registry.get(event_id)
            if not state or state.get('done'):
                return
            state['done'] = True
            analysis_registry.pop(event_id, None)

        raw_suspicious = bool(result.get('suspicious'))
        verdict = result.get('verdict') or 'BELİRSİZ'
        reason_text = result.get('reason') or 'Analiz tamamlandı.'
        analysis_status = 'suspicious' if raw_suspicious else 'clean'
        emit_channel = 'alert' if raw_suspicious else 'screenshot'

        # Kodlama modu periyodiklerde 2/3 doğrulama filtresi.
        if mode == 'coding' and reason in ('desktop_periodic', 'periodic'):
            votes = coding_vlm_votes[sid]
            confirmed_suspicious = record_coding_vote(votes, raw_suspicious)
            if raw_suspicious and not confirmed_suspicious:
                analysis_status = 'uncertain'
                verdict = 'BELİRSİZ'
                reason_text = f"{reason_text} (2/3 kuralı: tekrar teyit bekleniyor)"
                emit_channel = 'screenshot'
                raw_suspicious = False

        # VLM hatası/timeout-benzeri durumlar manuel incelemeye düşsün.
        if verdict == 'HATA':
            analysis_status = 'uncertain'
            emit_channel = 'screenshot'
            raw_suspicious = False

        if raw_suspicious:
            increase_alert_count(sid)

        result_payload = {
            **base_payload,
            'suspicious': raw_suspicious,
            'agentVerdict': verdict,
            'agentReason': reason_text,
            'analysisStatus': analysis_status
        }
        socketio.emit(emit_channel, result_payload, to='admins')

    analyze_async(screenshot, mode, on_agent_done, context=client_ctx)
    return jsonify({'success': True})

# ─────────────────────────────────────────
# GENEL UYARI
# ─────────────────────────────────────────
@app.route('/alert', methods=['POST'])
def receive_alert():
    data = request.json or {}
    sid = authenticated_student(data)
    if not sid:
        return jsonify({'success': False, 'message': 'Geçersiz öğrenci oturumu.'}), 401
    data.setdefault('student', {})['id'] = sid
    increase_alert_count(sid)
    payload = {
        **data,
        'eventId': data.get('eventId') or str(uuid.uuid4()),
        'analysisStatus': data.get('analysisStatus') or 'suspicious',
        'agentVerdict': data.get('agentVerdict') or 'ŞÜPHELİ',
        'agentReason': data.get('agentReason') or 'Kural tabanlı alarm.'
    }
    socketio.emit('alert', payload, to='admins')
    return jsonify({'success': True})

# ─────────────────────────────────────────
# SINAVIN YÖNETİMİ (socket events)
# ─────────────────────────────────────────
@socketio.on('start_exam')
def handle_start_exam(data):
    if not require_admin_socket():
        return
    urls = data.get('allowed_urls', []) or []
    cleaned = []
    seen = set()
    for url in urls:
        if not isinstance(url, str):
            continue
        u = url.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        cleaned.append(u)

    exam_state.update({
        'active':       True,
        'mode':         data.get('mode', 'web'),
        'duration':     data.get('duration', 90),
        'started_at':   now_iso(),
        'allowed_urls': cleaned,
        'exam_code':    data.get('exam_code', '')
    })
    students.clear()
    student_sessions.clear()
    coding_vlm_votes.clear()
    persist_state()
    socketio.emit('exam_started', public_exam_state())
    print(f"[Sınav] Başladı — mod:{exam_state['mode']} süre:{exam_state['duration']}dk")
    print(f"[Sınav] İzinli URL'ler: {exam_state['allowed_urls']}")

@socketio.on('stop_exam')
def handle_stop_exam(_):
    if not require_admin_socket():
        return
    exam_state['active'] = False
    student_sessions.clear()
    coding_vlm_votes.clear()
    persist_state()
    socketio.emit('exam_stopped', {})
    print("[Sınav] Durduruldu")

@socketio.on('update_duration')
def handle_update_duration(data):
    if not require_admin_socket():
        return
    exam_state['duration'] = data.get('duration', exam_state['duration'])
    persist_state()
    socketio.emit('duration_updated', {'duration': exam_state['duration']})

@socketio.on('change_mode')
def handle_change_mode(data):
    if not require_admin_socket():
        return
    exam_state['mode'] = data.get('mode', 'web')
    persist_state()
    socketio.emit('mode_changed', {'mode': exam_state['mode']})

@socketio.on('update_urls')
def handle_update_urls(data):
    if not require_admin_socket():
        return
    urls = data.get('allowed_urls', []) or []
    cleaned = []
    seen = set()
    for url in urls:
        if not isinstance(url, str):
            continue
        u = url.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        cleaned.append(u)
    exam_state['allowed_urls'] = cleaned
    persist_state()
    socketio.emit('urls_updated', {'allowed_urls': exam_state['allowed_urls']})
    print(f"[Sınav] URL'ler güncellendi: {exam_state['allowed_urls']}")

@socketio.on('update_exam_code')
def handle_update_exam_code(data):
    if not require_admin_socket():
        return
    code = (data.get('exam_code') or '').strip().upper()
    exam_state['exam_code'] = code
    persist_state()
    socketio.emit('exam_code_updated', {'exam_code': exam_state['exam_code']})
    print(f"[Sınav] Kod güncellendi: {exam_state['exam_code']}")

# ─────────────────────────────────────────
# STATİK + DURUM
# ─────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('dashboard', 'index.html')

@app.route('/state')
def get_state():
    return jsonify(public_exam_state())

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    supplied = get_bearer_token() or request.headers.get('X-Admin-Token', '')
    if not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        return jsonify({'success': False, 'message': 'Yetkisiz erişim.'}), 401
    return send_from_directory(SCREENSHOTS_DIR, filename)

@socketio.on('connect')
def on_connect(auth=None):
    supplied_token = str((auth or {}).get('adminToken', '') or '').strip()
    if supplied_token and secrets.compare_digest(supplied_token, ADMIN_TOKEN):
        admin_sockets.add(request.sid)
        join_room('admins')
        socketio.emit('admin_authenticated', {}, to=request.sid)
    elif supplied_token:
        socketio.emit(
            'admin_error',
            {'message': 'Öğretmen tokenı geçersiz.'},
            to=request.sid
        )
    socketio.emit('state_sync', public_exam_state(), to=request.sid)
    if request.sid in admin_sockets:
        socketio.emit('students_update', list(students.values()), to=request.sid)
    print('[ExamGuard] İstemci bağlandı')

@socketio.on('disconnect')
def on_disconnect():
    admin_sockets.discard(request.sid)

if __name__ == '__main__':
    print('=' * 50)
    print('  ExamGuard Backend')
    print('  Dashboard → http://localhost:5000')
    print(f'  Öğretmen tokenı → {ADMIN_TOKEN}')
    print('=' * 50)
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
