# ExamGuard - desktop_agent.py

import time
import threading
import io
import base64
import requests
import tkinter as tk
import pyautogui
import pystray
import socketio
import win32gui
import win32process
import psutil
from PIL import Image, ImageDraw

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
BACKEND_URL         = 'https://monitoragent-production.up.railway.app'
STATE_POLL_INTERVAL = 5
CODING_SS_INTERVAL  = 30
UNFOCUS_WAIT        = 1

# ─────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────
running           = True
exam_active       = False
exam_mode         = 'web'
student_info      = {}
session_token     = ''
last_coding_ss    = 0
chrome_was_active = False
tray_icon         = None
login_pending     = False   # giriş ekranı açık mı
tray_lock         = threading.Lock()

# SocketIO client
sio = socketio.Client(reconnection=True, reconnection_delay=3)

# ─────────────────────────────────────────
# GİRİŞ EKRANI (Tkinter)
# ─────────────────────────────────────────
def show_login():
    result = {'success': False}

    BG      = '#0d1117'
    SURFACE = '#161b22'
    BORDER  = '#30363d'
    ACCENT  = '#ef4444'
    TEXT    = '#e2e8f0'
    DIM     = '#94a3b8'
    MONO    = ('Courier New', 10)

    root = tk.Tk()
    root.title('ExamGuard')
    root.configure(bg=BG)
    root.resizable(False, False)

    # ── Başlık ──
    tk.Label(root, text='EXAMGUARD', font=('Courier New', 14, 'bold'),
             fg=ACCENT, bg=BG).pack(pady=(24, 2))
    tk.Label(root, text='Öğrenci Girişi', font=('Courier New', 9),
             fg=DIM, bg=BG).pack(pady=(0, 18))

    # ── Alan oluşturucu ──
    def field(parent, label, show=None):
        tk.Label(parent, text=label, font=('Courier New', 9),
                 fg=DIM, bg=BG, anchor='w').pack(fill='x')
        e = tk.Entry(parent, font=MONO, bg=SURFACE, fg=TEXT,
                     insertbackground=TEXT, relief='flat',
                     highlightthickness=1,
                     highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        if show:
            e.config(show=show)
        e.pack(fill='x', ipady=6, pady=(2, 14))
        return e

    form = tk.Frame(root, bg=BG)
    form.pack(padx=28, fill='x')

    e_name = field(form, 'Ad Soyad')
    e_id   = field(form, 'Öğrenci Numarası')
    e_code = field(form, 'Sınav Kodu', show='*')

    # ── Hata mesajı ──
    lbl_err = tk.Label(root, text='', font=('Courier New', 9),
                       fg=ACCENT, bg=BG, wraplength=200)
    lbl_err.pack(pady=(0, 6))

    # ── Buton ──
    def on_login():
        global session_token
        name = e_name.get().strip()
        sid  = e_id.get().strip()
        code = e_code.get().strip()

        if not name or not sid or not code:
            lbl_err.config(text='Tüm alanları doldurun.')
            return

        btn.config(state='disabled', text='Doğrulanıyor...')
        root.update()

        try:
            res  = requests.post(f'{BACKEND_URL}/student/verify',
                                 json={'name': name, 'id': sid, 'code': code},
                                 timeout=5)
            data = res.json()
            if data.get('success'):
                student_info.update({'name': name, 'id': sid})
                session_token = data.get('sessionToken', '')
                result['success'] = True
                root.destroy()
            else:
                lbl_err.config(text=data.get('message', 'Geçersiz bilgiler.'))
                btn.config(state='normal', text='GİRİŞ YAP')
        except Exception:
            lbl_err.config(text="Backend'e bağlanılamadı.")
            btn.config(state='normal', text='GİRİŞ YAP')

    btn = tk.Button(root, text='GİRİŞ YAP',
                    font=('Courier New', 10, 'bold'),
                    bg=ACCENT, fg='white', relief='flat',
                    activebackground='#dc2626', activeforeground='white',
                    cursor='hand2', command=on_login)
    btn.pack(padx=28, fill='x', ipady=8, pady=(0, 24))

    root.bind('<Return>', lambda e: on_login())

    # Ekranı ortala
    root.update_idletasks()
    w, h = 300, root.winfo_reqheight()
    x = (root.winfo_screenwidth()  - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f'{w}x{h}+{x}+{y}')

    root.mainloop()
    return result['success']


# ─────────────────────────────────────────
# TRAY İKONU
# ─────────────────────────────────────────
def create_icon(color='gray'):
    img  = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {'gray': '#4b5563', 'green': '#22c55e', 'red': '#ef4444'}
    draw.ellipse([4, 4, 60, 60], fill=colors.get(color, '#4b5563'))
    return img

def update_tray(color, tooltip):
    if not tray_icon:
        return
    try:
        with tray_lock:
            tray_icon.icon = create_icon(color)
            tray_icon.title = tooltip
    except OSError as e:
        print(f'[Agent] Tray güncelleme hatası: {e}')

def clear_student_session():
    global student_info, session_token, login_pending
    student_info = {}
    session_token = ''
    login_pending = False

# ─────────────────────────────────────────
# AKTİF PENCERE
# ─────────────────────────────────────────
def is_chrome_active():
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return 'chrome' in psutil.Process(pid).name().lower()
    except Exception:
        return False

def get_foreground_context():
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ''
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid)
        return {
            'activeWindowTitle': title,
            'activeProcess': proc.name(),
            'activePid': pid,
            'source': 'desktop_agent'
        }
    except Exception:
        return {
            'activeWindowTitle': '',
            'activeProcess': '',
            'activePid': None,
            'source': 'desktop_agent'
        }

# ─────────────────────────────────────────
# EKRAN GÖRÜNTÜSÜ
# ─────────────────────────────────────────
def take_screenshot_b64():
    try:
        ss     = pyautogui.screenshot()
        buffer = io.BytesIO()
        ss.save(buffer, format='JPEG', quality=75)
        return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()
    except Exception as e:
        print(f'[Agent] SS hatası: {e}')
        return None

def send_screenshot(reason):
    if not session_token or not student_info:
        ensure_login_prompt()
        return
    ss = take_screenshot_b64()
    if not ss:
        return
    context = get_foreground_context()
    try:
        response = requests.post(f'{BACKEND_URL}/screenshot', headers={
            'Authorization': f'Bearer {session_token}'
        }, json={
            'screenshot': ss,
            'reason':     reason,
            'details':    'Desktop agent',
            'student':    student_info,
            'timestamp':  time.strftime('%Y-%m-%dT%H:%M:%S'),
            'tabUrl':     '',
            'tabTitle':   'Masaüstü',
            'mode':       exam_mode,
            'clientContext': context
        }, timeout=10)
        if response.status_code == 401:
            clear_student_session()
            update_tray('gray', 'ExamGuard — Oturum Geçersiz')
            ensure_login_prompt()
        response.raise_for_status()
        print(f'[Agent] SS gönderildi: {reason}')
    except Exception as e:
        print(f'[Agent] Gönderme hatası: {e}')

# ─────────────────────────────────────────
# WEBSOCKET EVENTS
# ─────────────────────────────────────────
@sio.event
def connect():
    print('[Agent] Backend bağlantısı kuruldu')
    update_tray('gray', 'ExamGuard — Bekleniyor')

@sio.event
def disconnect():
    print('[Agent] Backend bağlantısı kesildi')
    update_tray('gray', 'ExamGuard — Bağlantı Yok')

@sio.on('exam_started')
def on_exam_started(data):
    global exam_active, exam_mode, login_pending
    exam_active = True
    exam_mode   = data.get('mode', 'web')
    print(f'[Agent] Sınav başladı — mod: {exam_mode}')
    update_tray('gray', 'ExamGuard — Giriş Bekleniyor')

    if not student_info and not login_pending:
        login_pending = True
        threading.Thread(target=open_login_screen, daemon=True).start()

@sio.on('exam_stopped')
def on_exam_stopped(_=None):
    global exam_active
    exam_active   = False
    clear_student_session()
    update_tray('gray', 'ExamGuard — Bekliyor')
    print('[Agent] Sınav durduruldu')

@sio.on('mode_changed')
def on_mode_changed(data):
    global exam_mode
    exam_mode = data.get('mode', 'web')
    print(f'[Agent] Mod değişti: {exam_mode}')

def open_login_screen():
    global login_pending
    success = show_login()
    login_pending = False
    if success:
        update_tray('green', f'ExamGuard — AKTİF ({exam_mode})')
    else:
        update_tray('gray', 'ExamGuard — Giriş İptal')

def ensure_login_prompt():
    global login_pending
    if exam_active and not student_info and not login_pending:
        login_pending = True
        threading.Thread(target=open_login_screen, daemon=True).start()

def connect_to_backend():
    while running:
        try:
            if not sio.connected:
                sio.connect(
                    BACKEND_URL,
                    wait_timeout=15
                )
            while running and sio.connected:
                time.sleep(1)
        except Exception as e:
            print(f'[Agent] Bağlantı hatası: {e}')
        if running:
            time.sleep(5)

# ─────────────────────────────────────────
# STATE KONTROLÜ (fallback polling)
# ─────────────────────────────────────────
def fetch_state():
    global exam_active, exam_mode
    try:
        data       = requests.get(f'{BACKEND_URL}/state', timeout=5).json()
        exam_active = data.get('active', False)
        exam_mode   = data.get('mode', 'web')
        if not exam_active:
            clear_student_session()
        return True
    except Exception:
        return False

# ─────────────────────────────────────────
# ANA DÖNGÜ
# ─────────────────────────────────────────
def monitoring_loop():
    global chrome_was_active, last_coding_ss, running

    last_state_check = 0

    while running:
        now = time.time()

        if now - last_state_check >= STATE_POLL_INTERVAL:
            ok = fetch_state()
            last_state_check = now
            if ok:
                if exam_active:
                    update_tray('green', f'ExamGuard — AKTİF ({exam_mode})')
                else:
                    update_tray('gray', 'ExamGuard — Bekliyor')
                    chrome_was_active = False
                    last_coding_ss    = 0

        if not exam_active:
            time.sleep(1)
            continue

        ensure_login_prompt()

        if exam_mode == 'web':
            chrome_now = is_chrome_active()
            if chrome_was_active and not chrome_now:
                time.sleep(UNFOCUS_WAIT)
                send_screenshot('desktop_unfocused')
                update_tray('red', 'ExamGuard — Şüpheli Hareket!')
                time.sleep(2)
                update_tray('green', f'ExamGuard — AKTİF ({exam_mode})')
            chrome_was_active = chrome_now

        elif exam_mode == 'coding':
            if now - last_coding_ss >= CODING_SS_INTERVAL:
                send_screenshot('desktop_periodic')
                last_coding_ss = now

        time.sleep(1)

# ─────────────────────────────────────────
# TRAY MENÜSÜ
# ─────────────────────────────────────────
def on_quit(icon, item):
    global running
    running = False
    if sio.connected:
        sio.disconnect()
    icon.stop()

def setup_tray():
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem('ExamGuard Desktop Agent', None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Çıkış', on_quit)
    )
    tray_icon = pystray.Icon('ExamGuard', create_icon('gray'),
                              'ExamGuard — Bekliyor', menu)
    return tray_icon

# ─────────────────────────────────────────
# BAŞLANGIÇ
# ─────────────────────────────────────────
if __name__ == '__main__':
    print('[ExamGuard Desktop Agent] Başlatılıyor...')
    print(f'[Agent] Backend: {BACKEND_URL}')
    print('[Agent] Sınav başlaması bekleniyor...')

    # WebSocket bağlantısı
    threading.Thread(target=connect_to_backend, daemon=True).start()

    # İzleme thread'i
    threading.Thread(target=monitoring_loop, daemon=True).start()

    # Tray ikonu (ana thread)
    setup_tray().run()
