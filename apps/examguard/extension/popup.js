// ExamGuard - popup.js (sadece bilgi gösterir)

let timerInterval = null;
let currentState = null;
const BACKEND_URL = 'http://localhost:5000';

function updateUI(state) {
  currentState = state;
  const dot        = document.getElementById('dot');
  const statusText = document.getElementById('statusText');
  const examInfo   = document.getElementById('examInfo');
  const idleMsg    = document.getElementById('idleMsg');
  const infoMode   = document.getElementById('infoMode');
  const timerVal   = document.getElementById('timerVal');
  const loginForm  = document.getElementById('loginForm');

  if (state && state.active) {
    chrome.runtime.sendMessage({ type: 'GET_STATUS' }, (status) => {
      const loggedIn = !!(status && status.studentInfo && status.sessionToken);
      loginForm.style.display = loggedIn ? 'none' : 'block';
      idleMsg.style.display = loggedIn ? 'none' : 'block';
      idleMsg.textContent = loggedIn ? '' : 'Öğrenci girişi gerekli';
    });
    dot.className        = 'dot active';
    statusText.className = 'status-text active';
    statusText.textContent = 'AKTİF';
    examInfo.style.display = 'block';
    idleMsg.style.display  = 'none';

    infoMode.textContent = state.mode === 'coding' ? '💻 Kodlama' : '🌐 Web';

    // Kalan süreyi hesapla
    if (state.started_at && state.duration) {
      startCountdown(state.started_at, state.duration);
    }
  } else {
    dot.className        = 'dot idle';
    statusText.className = 'status-text idle';
    statusText.textContent = 'BEKLİYOR';
    examInfo.style.display = 'none';
    idleMsg.style.display  = 'block';
    loginForm.style.display = 'none';
    if (timerInterval) clearInterval(timerInterval);
  }
}

function startCountdown(startedAt, durationMin) {
  const timerVal = document.getElementById('timerVal');
  if (timerInterval) clearInterval(timerInterval);

  timerInterval = setInterval(() => {
    const start     = new Date(startedAt).getTime();
    const endTime   = start + durationMin * 60 * 1000;
    const remaining = endTime - Date.now();

    if (remaining <= 0) {
      timerVal.textContent = '00:00';
      timerVal.className   = 'timer-value low';
      clearInterval(timerInterval);
      return;
    }

    const mins = Math.floor(remaining / 60000);
    const secs = Math.floor((remaining % 60000) / 1000);
    timerVal.textContent = `${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
    timerVal.className   = remaining < 5 * 60000 ? 'timer-value low' : 'timer-value';
  }, 1000);
}

// Backend'den state çek
document.getElementById('loginButton').addEventListener('click', async () => {
  const name = document.getElementById('studentName').value.trim();
  const id = document.getElementById('studentId').value.trim();
  const code = document.getElementById('examCode').value.trim();
  const error = document.getElementById('loginError');
  if (!name || !id || !code) {
    error.textContent = 'Tüm alanları doldurun.';
    return;
  }
  try {
    const response = await fetch(`${BACKEND_URL}/student/verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, id, code })
    });
    const data = await response.json();
    if (!data.success) {
      error.textContent = data.message || 'Giriş başarısız.';
      return;
    }
    chrome.runtime.sendMessage({
      type: 'START_EXAM',
      mode: currentState?.mode || 'web',
      allowed_urls: currentState?.allowed_urls || [],
      student: { name, id },
      sessionToken: data.sessionToken
    }, () => window.close());
  } catch {
    error.textContent = "Backend'e bağlanılamadı.";
  }
});

fetch(`${BACKEND_URL}/state`)
  .then(r => r.json())
  .then(updateUI)
  .catch(() => updateUI(null));
