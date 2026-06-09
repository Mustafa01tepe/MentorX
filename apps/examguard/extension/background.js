// ExamGuard - background.js

const BACKEND_URL = 'https://monitoragent-production.up.railway.app';
const SCREENSHOT_INTERVAL_SECONDS = 30;
const STATE_CHECK_INTERVAL_SECONDS = 10;
const UNFOCUS_CAPTURE_COOLDOWN_MS = 7000;

// Hoca dashboard'dan gönderir, başlangıçta boş
let allowedUrls = [];
let examActive  = false;
let studentInfo = null;
let sessionToken = null;
let examMode    = 'web';
let lastUnfocusCaptureAt = 0;

const AI_EXTENSION_BLACKLIST = [
  { id: 'camppjleccjaphfdbohjdohecfnoikec', name: 'Merlin AI' },
  { id: 'ofpnmcalabcbjgholdjcjblkibolbppb', name: 'Monica' },
  { id: 'difoiogjjojoaoomphldepapgpbgkhkb', name: 'Sider' },
  { id: 'eppiocemhmnlbhjplcgkofciiegomcon', name: 'ChatGPT Writer' },
  { id: 'lnangelmahmmcjjhemaepkihjcgkiobg', name: 'WebChatGPT' },
  { id: 'klbifljdmjgjimlmooibogcnhfalpkck', name: 'Perplexity AI' },
  { id: 'ennpfpdlacihbcjbelmjoanfkpplbdde', name: 'Copilot' },
  { id: 'bgnkhhnnamicmpeenaelnjfhikgbkllg', name: 'ChatGPT' },
];

// ─────────────────────────────────────────
// BAŞLANGIÇ — backend'den state çek
// ─────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  initializeGuard();
});

chrome.runtime.onStartup.addListener(() => {
  initializeGuard();
});

initializeGuard();

function ensureStateCheckAlarm() {
  chrome.alarms.create('stateCheck', {
    periodInMinutes: STATE_CHECK_INTERVAL_SECONDS / 60
  });
}

function ensureExamAlarms() {
  chrome.alarms.create('periodicScreenshot', {
    periodInMinutes: SCREENSHOT_INTERVAL_SECONDS / 60
  });
  chrome.alarms.create('heartbeat', { periodInMinutes: 1 });
}

async function activateContentGuards() {
  const tabs = await chrome.tabs.query({});
  await Promise.all(tabs
    .filter(tab => Number.isInteger(tab.id))
    .map(tab => chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ['content.js']
    }).catch(() => {})));
}

async function initializeGuard() {
  ensureStateCheckAlarm();
  const stored = await chrome.storage.local.get([
    'examActive', 'studentInfo', 'sessionToken'
  ]);
  examActive = !!stored.examActive;
  studentInfo = stored.studentInfo || null;
  sessionToken = stored.sessionToken || null;
  await checkExamState();
}

// ─────────────────────────────────────────
// MESAJ ALICI (popup'tan)
// ─────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {

  if (message.type === 'START_EXAM') {
    examActive   = true;
    examMode     = message.mode || 'web';
    allowedUrls  = message.allowed_urls || [];
    studentInfo  = message.student;
    sessionToken = message.sessionToken;
    chrome.storage.local.set({
      examActive: true,
      studentInfo: message.student,
      sessionToken
    });

    chrome.alarms.clearAll();
    ensureExamAlarms();
    ensureStateCheckAlarm();

    scanAIExtensions();
    studentJoin(message.student);
    activateContentGuards();
    sendResponse({ success: true });
  }

  if (message.type === 'STOP_EXAM') {
    studentLeave(studentInfo);
    examActive  = false;
    studentInfo = null;
    sessionToken = null;
    allowedUrls = [];
    lastUnfocusCaptureAt = 0;
    chrome.alarms.clearAll();
    ensureStateCheckAlarm();
    chrome.storage.local.set({
      examActive: false,
      studentInfo: null,
      sessionToken: null
    });
    sendResponse({ success: true });
  }

  if (message.type === 'GET_STATUS') {
    sendResponse({ examActive, studentInfo, sessionToken, examMode, allowedUrls });
  }

  // Dashboard'dan URL güncellemesi gelirse
  if (message.type === 'UPDATE_URLS') {
    allowedUrls = message.allowed_urls || [];
    sendResponse({ success: true });
  }

  if (message.type === 'UPDATE_MODE') {
    examMode = message.mode;
    sendResponse({ success: true });
  }

  // Content script görünürlük kaybını bildirdiğinde (tab arka plana düştü)
  if (message.type === 'PAGE_HIDDEN') {
    if (examActive) {
      captureUnfocusEvidence('Sayfa görünürlüğü kayboldu (document.hidden)')
        .catch(() => {});
    }
    sendResponse({ success: true });
  }

  return true;
});

// ─────────────────────────────────────────
// ALARMLAR
// ─────────────────────────────────────────
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'stateCheck') {
    checkExamState();
    return;
  }
  if (!examActive) return;
  if (alarm.name === 'periodicScreenshot') captureAndSend('periodic');
  if (alarm.name === 'heartbeat')          sendHeartbeat();
});

// Her 10 saniyede backend state kontrolü
async function checkExamState() {
  try {
    const res  = await fetch(`${BACKEND_URL}/state`);
    const data = await res.json();

    // Backend'de aktif sınav yoksa local state'i netleştir
    if (!data.active) {
      const wasActive = examActive;
      examActive  = false;
      studentInfo = null;
      sessionToken = null;
      allowedUrls = [];
      lastUnfocusCaptureAt = 0;
      chrome.storage.local.set({
        examActive: false,
        studentInfo: null,
        sessionToken: null
      });
      chrome.alarms.clear('periodicScreenshot');
      chrome.alarms.clear('heartbeat');
      ensureStateCheckAlarm();

      // Aktif sınavdan pasife düştüyse bitiş popup'u göster
      if (wasActive) {
        chrome.tabs.query({}, (tabs) => {
          tabs.forEach(tab => {
            chrome.scripting.executeScript({
              target: { tabId: tab.id },
              func: showExamEndPopup
            }).catch(() => {});
          });
        });
      }
      return;
    }

    // Backend başladı, mod veya URL değiştiyse güncelle
    if (data.active && examActive) {
      examMode    = data.mode         || examMode;
      allowedUrls = data.allowed_urls || allowedUrls;
      ensureExamAlarms();
    }

    // Sınav yeni başladıysa (extension henüz aktif değil)
    if (data.active && !examActive && studentInfo && sessionToken) {
      examActive  = true;
      examMode    = data.mode || 'web';
      allowedUrls = data.allowed_urls || [];
      chrome.storage.local.set({ examActive: true });
      ensureExamAlarms();
      scanAIExtensions();
      activateContentGuards();
    }

  } catch (e) { /* backend kapalı, sorun değil */ }
}

// ─────────────────────────────────────────
// SEKME DEĞİŞİMİ (sekmeler arası)
// ─────────────────────────────────────────
chrome.tabs.onActivated.addListener(async (activeInfo) => {
  if (!examActive) return;
  try {
    const tab = await chrome.tabs.get(activeInfo.tabId);
    await checkAndBlock(activeInfo.tabId, tab.url || '');
  } catch (e) { console.error('[ExamGuard] Tab activated error:', e); }
});

// ─────────────────────────────────────────
// URL DEĞİŞİMİ (aynı sekmede başka URL)
// ─────────────────────────────────────────
const lastTabUrls = {};
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (!examActive) return;
  if (changeInfo.status !== 'complete') return;
  const url = tab.url || '';
  // Aynı URL'de tekrar tetiklenmeyi önle
  if (lastTabUrls[tabId] === url) return;
  lastTabUrls[tabId] = url;
  await checkAndBlock(tabId, url);
});

// Ortak kontrol + engelleme
async function checkAndBlock(tabId, url) {
  if (!url) return;
  if (isAllowed(url)) return;
  await captureAndSend('tab_switch', url);
  const target = allowedUrls[0] || 'https://moodle.erzurum.edu.tr/';
  chrome.tabs.update(tabId, { url: target });
}

async function captureUnfocusEvidence(details = '') {
  const now = Date.now();
  if (now - lastUnfocusCaptureAt < UNFOCUS_CAPTURE_COOLDOWN_MS) return;
  lastUnfocusCaptureAt = now;
  await captureAndSend('window_unfocused', details || 'Chrome arka plana atıldı');
}

// ─────────────────────────────────────────
// YENİ SEKME AÇMA
// ─────────────────────────────────────────
chrome.tabs.onCreated.addListener(async (tab) => {
  if (!examActive) return;
  await captureAndSend('new_tab_attempt', tab.pendingUrl || tab.url || 'yeni sekme');
  setTimeout(() => chrome.tabs.remove(tab.id).catch(() => {}), 200);
});

// ─────────────────────────────────────────
// YENİ PENCERE AÇMA
// ─────────────────────────────────────────
chrome.windows.onCreated.addListener(async (win) => {
  if (!examActive) return;
  // Extension popup pencerelerini atla
  if (win.type === 'popup') return;
  await captureAndSend('new_tab_attempt', 'Yeni pencere açma teşebbüsü');
  setTimeout(() => chrome.windows.remove(win.id).catch(() => {}), 200);
});

// ─────────────────────────────────────────
// CHROME ARKA PLANA ATILDI
// ─────────────────────────────────────────
chrome.windows.onFocusChanged.addListener(async (windowId) => {
  if (!examActive) return;
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    // 1 saniye bekle — öğrenci nereye geçtiği belli olsun
    await new Promise(r => setTimeout(r, 1000));
    await captureUnfocusEvidence('Chrome arka plana atıldı');
  }
});

// ─────────────────────────────────────────
// SINAVIN BİTİŞİ — backend'den sinyal
// ─────────────────────────────────────────
chrome.storage.onChanged.addListener((changes) => {
  if (changes.examActive && changes.examActive.newValue === false) {
    // Tüm aktif sekmelere popup mesajı gönder
    chrome.tabs.query({}, (tabs) => {
      tabs.forEach(tab => {
        chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: showExamEndPopup
        }).catch(() => {});
      });
    });
  }
});

function showExamEndPopup() {
  const overlay = document.createElement('div');
  overlay.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:2147483647',
    'background:rgba(8,12,16,0.96)',
    'display:flex', 'align-items:center', 'justify-content:center',
    "font-family:'IBM Plex Mono',monospace"
  ].join(';');
  overlay.innerHTML = [
    '<div style="text-align:center;color:#e2e8f0">',
    '<div style="font-size:48px;margin-bottom:16px">&#x2705;</div>',
    '<div style="font-size:22px;font-weight:600;color:#22c55e;letter-spacing:2px;margin-bottom:8px">SINAV TAMAMLANDI</div>',
    '<div style="font-size:13px;color:#94a3b8;letter-spacing:1px">',
    'Gozetmen sinavi sonlandirdi.<br>Bilgisayarinizi birakabilirsiniz.',
    '</div></div>'
  ].join('');
  document.body.appendChild(overlay);
}

// ─────────────────────────────────────────
// URL KONTROL — izinli mi?
// ─────────────────────────────────────────
function normalizePathname(pathname) {
  if (!pathname) return '/';
  const normalized = pathname.replace(/\/+$/, '');
  return normalized || '/';
}

function parseUrl(raw) {
  try {
    const u = new URL((raw || '').trim());
    return {
      origin: u.origin.toLowerCase(),
      pathname: normalizePathname(u.pathname),
      hostname: (u.hostname || '').toLowerCase()
    };
  } catch {
    return null;
  }
}

function isAllowed(url) {
  if (!url || url.startsWith('chrome://') || url.startsWith('about:')) return false;
  const current = parseUrl(url);
  if (!current) return false;

  // Hoca URL girmediyse Moodle domain'ine izin ver
  if (allowedUrls.length === 0) {
    return current.hostname.includes('moodle.erzurum.edu.tr');
  }

  // Hoca URL girdiyse bu iki URL (ve alt-path'leri) arasında gezinmeye izin ver.
  return allowedUrls.some((allowedRaw) => {
    const allowed = parseUrl(allowedRaw);
    if (!allowed) return false;
    if (current.origin !== allowed.origin) return false;

    if (allowed.pathname === '/') return true;
    return (
      current.pathname === allowed.pathname ||
      current.pathname.startsWith(`${allowed.pathname}/`)
    );
  });
}

// ─────────────────────────────────────────
// EKRAN GÖRÜNTÜSÜ AL + GÖNDER
// ─────────────────────────────────────────
async function captureAndSend(reason, details = '') {
  try {
    // Service worker bağlamında odak kaybında "currentWindow" boş kalabilir.
    // Bu yüzden son odaklı pencereden aktif tab alınır.
    let [activeTab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!activeTab) {
      [activeTab] = await chrome.tabs.query({ active: true });
    }
    if (!activeTab) return;

    const targetWindowId = Number.isInteger(activeTab.windowId) ? activeTab.windowId : undefined;
    const screenshot = await chrome.tabs.captureVisibleTab(targetWindowId, {
      format: 'jpeg', quality: 75
    });

    await fetch(`${BACKEND_URL}/screenshot`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        screenshot,
        reason,
        details,
        student:   studentInfo,
        timestamp: new Date().toISOString(),
        tabUrl:    activeTab.url   || '',
        tabTitle:  activeTab.title || '',
        mode:      examMode,
        sessionToken,
        clientContext: {
          source: 'extension',
          activeWindowTitle: activeTab.title || '',
          activeProcess: 'chrome.exe'
        }
      })
    });
  } catch (err) {
    console.error('[ExamGuard] Capture error:', err);
  }
}

// ─────────────────────────────────────────
// ÖĞRENCİ KATILIM / AYRILMA / HEARTBEAT
// ─────────────────────────────────────────
async function studentJoin(student) {
  try {
    await fetch(`${BACKEND_URL}/student/join`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({ student, timestamp: new Date().toISOString() })
    });
  } catch (e) { console.error('[ExamGuard] Join error:', e); }
}

async function studentLeave(student) {
  if (!student) return;
  try {
    await fetch(`${BACKEND_URL}/student/leave`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({ student, timestamp: new Date().toISOString() })
    });
  } catch (e) { console.error('[ExamGuard] Leave error:', e); }
}

async function sendHeartbeat() {
  if (!studentInfo) return;
  try {
    await fetch(`${BACKEND_URL}/student/heartbeat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({ student: studentInfo, timestamp: new Date().toISOString() })
    });
  } catch (e) { console.error('[ExamGuard] Heartbeat error:', e); }
}

// ─────────────────────────────────────────
// AI EKLENTİ TARAMASI
// ─────────────────────────────────────────
async function scanAIExtensions() {
  try {
    const all   = await chrome.management.getAll();
    const found = [];
    for (const ext of all) {
      const match = AI_EXTENSION_BLACKLIST.find(b => b.id === ext.id);
      if (match && ext.enabled) found.push({
        id: ext.id, blacklistName: match.name, installedName: ext.name
      });
    }
    if (found.length > 0) {
      await fetch(`${BACKEND_URL}/alert`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${sessionToken}`
        },
        body: JSON.stringify({
          type: 'ai_extension_detected',
          extensions: found,
          student:    studentInfo,
          timestamp:  new Date().toISOString()
        })
      });
    }
  } catch (e) { console.error('[ExamGuard] Scan error:', e); }
}
