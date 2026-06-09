// ExamGuard - background.js

const BACKEND_URL = 'https://monitoragent-production.up.railway.app';
const SCREENSHOT_INTERVAL_SECONDS = 30;
const STATE_CHECK_INTERVAL_SECONDS = 30;
const UNFOCUS_CAPTURE_DELAY_MS = 3000;
const UNFOCUS_CAPTURE_COOLDOWN_MS = 7000;
const DESKTOP_BRIDGE_URL = 'http://127.0.0.1:17843/session';
const DESKTOP_PAIRING_POLL_MS = 3000;

// Hoca dashboard'dan gönderir, başlangıçta boş
let allowedUrls = [];
let examActive  = false;
let studentInfo = null;
let sessionToken = null;
let examId       = null;
let examMode    = 'web';
let examStartedAt = null;
let examDuration = null;
let remoteExamActive = false;
let lastUnfocusCaptureAt = 0;
let lastSuccessfulScreenshot = null;
let windowUnfocusTimer = null;
const pageUnfocusTimers = new Map();
let backendConnected = false;
let lastSyncError = '';
let syncInProgress = null;
let pairingInProgress = null;

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

async function updateActionState() {
  if (!backendConnected) {
    await chrome.action.setBadgeText({ text: '!' });
    await chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
    await chrome.action.setTitle({ title: 'ExamGuard - Backend bağlantısı yok' });
    return;
  }
  if (examActive && sessionToken) {
    await chrome.action.setBadgeText({ text: 'ON' });
    await chrome.action.setBadgeBackgroundColor({ color: '#16a34a' });
    await chrome.action.setTitle({ title: 'ExamGuard - İzleme aktif' });
    return;
  }
  if (remoteExamActive) {
    await chrome.action.setBadgeText({ text: 'GİR' });
    await chrome.action.setBadgeBackgroundColor({ color: '#f59e0b' });
    await chrome.action.setTitle({ title: 'ExamGuard - Sınav aktif, öğrenci girişi gerekli' });
    return;
  }
  await chrome.action.setBadgeText({ text: '' });
  await chrome.action.setTitle({ title: 'ExamGuard - Sınav bekleniyor' });
}

function ensureExamAlarms() {
  chrome.alarms.create('periodicScreenshot', {
    periodInMinutes: SCREENSHOT_INTERVAL_SECONDS / 60
  });
  chrome.alarms.create('heartbeat', { periodInMinutes: 1 });
}

async function scheduleExamEndAlarm(startedAt, durationMinutes) {
  await chrome.alarms.clear('examEnd');
  if (!startedAt || durationMinutes === null || durationMinutes === undefined) return;
  const startTime = new Date(startedAt).getTime();
  const durationMs = Number(durationMinutes) * 60 * 1000;
  const endTime = startTime + durationMs;
  if (!Number.isFinite(endTime)) return;
  chrome.alarms.create('examEnd', { when: Math.max(Date.now() + 1000, endTime) });
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

async function enforceOpenTabs() {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (!Number.isInteger(tab.id) || !tab.url) continue;
    if (
      tab.url.startsWith('chrome://') ||
      tab.url.startsWith('chrome-extension://') ||
      tab.url.startsWith('about:')
    ) {
      continue;
    }
    await checkAndBlock(tab.id, tab.url);
  }
}

async function initializeGuard() {
  ensureStateCheckAlarm();
  const stored = await chrome.storage.local.get([
    'examActive', 'studentInfo', 'sessionToken', 'examId',
    'examStartedAt', 'examDuration'
  ]);
  examActive = !!stored.examActive;
  studentInfo = stored.studentInfo || null;
  sessionToken = stored.sessionToken || null;
  examId = stored.examId || null;
  examStartedAt = stored.examStartedAt || null;
  examDuration = stored.examDuration ?? null;
  await checkExamState();
}

async function clearLocalSession() {
  examActive = false;
  studentInfo = null;
  sessionToken = null;
  examId = null;
  examStartedAt = null;
  examDuration = null;
  allowedUrls = [];
  lastUnfocusCaptureAt = 0;
  lastSuccessfulScreenshot = null;
  clearWindowUnfocusTimer();
  clearPageUnfocusTimers();
  await chrome.storage.local.set({
    examActive: false,
    studentInfo: null,
    sessionToken: null,
    examId: null,
    examStartedAt: null,
    examDuration: null
  });
  await chrome.alarms.clear('periodicScreenshot');
  await chrome.alarms.clear('heartbeat');
  await chrome.alarms.clear('examEnd');
  ensureStateCheckAlarm();
  await updateActionState();
}

async function validateStudentSession(expectedExamId) {
  if (!studentInfo || !sessionToken) return false;
  const response = await fetch(`${BACKEND_URL}/student/session`, {
    headers: { 'Authorization': `Bearer ${sessionToken}` }
  });
  if (!response.ok) return false;
  const status = await response.json();
  return (
    status.success === true &&
    status.examActive === true &&
    status.examId === expectedExamId &&
    status.studentId === studentInfo.id
  );
}

async function startExamSession(message) {
  examActive = true;
  examMode = message.mode || 'web';
  allowedUrls = message.allowed_urls || [];
  studentInfo = message.student;
  sessionToken = message.sessionToken;
  examId = message.examId;
  examStartedAt = message.started_at || null;
  examDuration = message.duration ?? null;
  remoteExamActive = true;
  await chrome.storage.local.set({
    examActive: true,
    studentInfo,
    sessionToken,
    examId,
    examStartedAt,
    examDuration
  });

  await chrome.alarms.clearAll();
  ensureExamAlarms();
  ensureStateCheckAlarm();
  await scheduleExamEndAlarm(examStartedAt, examDuration);

  const joined = await studentJoin(studentInfo);
  if (!joined) {
    await clearLocalSession();
    return { success: false };
  }

  await scanAIExtensions();
  await activateContentGuards();
  await captureAndSend('periodic', 'Sınav girişi ilk kontrol');
  await enforceOpenTabs();
  await updateActionState();
  return { success: true };
}

async function tryDesktopPairing(remoteState = null) {
  if (!remoteExamActive || examActive || sessionToken) return false;
  if (pairingInProgress) return pairingInProgress;

  pairingInProgress = (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1500);
    try {
      const bridgeResponse = await fetch(DESKTOP_BRIDGE_URL, {
        cache: 'no-store',
        signal: controller.signal
      });
      if (bridgeResponse.status === 204 || !bridgeResponse.ok) return false;
      const pairing = await bridgeResponse.json();
      const expectedExamId = remoteState?.exam_id || examId;
      if (
        !pairing.pairingCode ||
        (expectedExamId && pairing.examId !== expectedExamId)
      ) {
        return false;
      }

      const exchangeResponse = await fetch(
        `${BACKEND_URL}/student/pairing/exchange`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pairingCode: pairing.pairingCode })
        }
      );
      if (!exchangeResponse.ok) return false;
      const exchange = await exchangeResponse.json();
      if (!exchange.success || !exchange.sessionToken || !exchange.student) {
        return false;
      }

      const state = remoteState || {};
      const result = await startExamSession({
        student: exchange.student,
        sessionToken: exchange.sessionToken,
        examId: exchange.examId,
        mode: state.mode || examMode,
        allowed_urls: state.allowed_urls || allowedUrls,
        started_at: state.started_at || examStartedAt,
        duration: state.duration ?? examDuration
      });
      if (result.success) {
        console.log('[ExamGuard] Masaüstü ajanı ile tek giriş tamamlandı');
      }
      return result.success;
    } catch (error) {
      if (error?.name !== 'AbortError') {
        console.debug('[ExamGuard] Masaüstü eşleştirmesi bekleniyor:', error);
      }
      return false;
    } finally {
      clearTimeout(timeout);
    }
  })();

  try {
    return await pairingInProgress;
  } finally {
    pairingInProgress = null;
  }
}

async function syncWithBackend() {
  if (syncInProgress) return syncInProgress;
  syncInProgress = (async () => {
    let lastError = null;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        await checkExamState();
        if (backendConnected) return true;
      } catch (error) {
        lastError = error;
      }
      if (attempt < 3) {
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
    if (lastError) throw lastError;
    return false;
  })();

  try {
    return await syncInProgress;
  } finally {
    syncInProgress = null;
  }
}

// ─────────────────────────────────────────
// MESAJ ALICI (popup'tan)
// ─────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {

  if (message.type === 'START_EXAM') {
    startExamSession(message)
      .then(sendResponse)
      .catch((error) => {
        console.error('[ExamGuard] Start session error:', error);
        sendResponse({ success: false, message: error?.message });
      });
  }

  if (message.type === 'STOP_EXAM') {
    studentLeave(studentInfo);
    chrome.alarms.clearAll();
    clearLocalSession();
    sendResponse({ success: true });
  }

  if (message.type === 'GET_STATUS') {
    sendResponse({
      examActive, studentInfo, sessionToken, examId, examMode, allowedUrls,
      examStartedAt, examDuration,
      backendConnected, lastSyncError, remoteExamActive
    });
  }

  if (message.type === 'SYNC_NOW') {
    syncWithBackend().then(() => {
      sendResponse({
        examActive, studentInfo, sessionToken, examId, examMode, allowedUrls,
        backendConnected, lastSyncError, remoteExamActive
      });
    }).catch((error) => {
      sendResponse({
        examActive, studentInfo, sessionToken, examId, examMode, allowedUrls,
        backendConnected: false,
        lastSyncError: error?.message || lastSyncError,
        remoteExamActive
      });
    });
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

  // Sekme 3 saniye boyunca görünmez kalırsa kanıt al.
  if (message.type === 'PAGE_VISIBILITY_CHANGED') {
    const tabId = sender.tab?.id;
    if (Number.isInteger(tabId)) {
      if (examActive && message.hidden === true) {
        schedulePageUnfocusCapture(tabId);
      } else {
        clearPageUnfocusTimer(tabId);
      }
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
  if (alarm.name === 'examEnd') {
    clearLocalSession();
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
    if (!res.ok) throw new Error(`State HTTP ${res.status}`);
    const data = await res.json();
    backendConnected = true;
    lastSyncError = '';
    remoteExamActive = !!data.active;

    // Backend'de aktif sınav yoksa local state'i netleştir
    if (!data.active) {
      const wasActive = examActive;
      await clearLocalSession();

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
      await updateActionState();
      return;
    }

    const remoteExamId = data.exam_id || null;
    examMode = data.mode || 'web';
    allowedUrls = data.allowed_urls || [];
    examStartedAt = data.started_at || null;
    examDuration = data.duration ?? null;
    await chrome.storage.local.set({ examStartedAt, examDuration });
    await scheduleExamEndAlarm(examStartedAt, examDuration);

    if (examId && remoteExamId && examId !== remoteExamId) {
      await clearLocalSession();
      await updateActionState();
      return;
    }

    if (sessionToken) {
      const sessionValid = await validateStudentSession(remoteExamId);
      if (!sessionValid) {
        await clearLocalSession();
        await updateActionState();
        return;
      }
    }

    if (!examActive && !sessionToken) {
      await tryDesktopPairing(data);
    }

    // Backend başladı, mod veya URL değiştiyse güncelle
    if (data.active && examActive) {
      ensureExamAlarms();
    }

    // Sınav yeni başladıysa (extension henüz aktif değil)
    if (data.active && !examActive && studentInfo && sessionToken) {
      examActive  = true;
      examId = remoteExamId;
      chrome.storage.local.set({ examActive: true, examId });
      ensureExamAlarms();
      scanAIExtensions();
      activateContentGuards();
      captureAndSend('periodic', 'Oturum geri yüklendi');
      enforceOpenTabs();
    }
    await updateActionState();

  } catch (e) {
    backendConnected = false;
    lastSyncError = e?.message || 'Backend bağlantısı kurulamadı.';
    await updateActionState();
    console.error('[ExamGuard] State sync error:', e);
  }
}

setInterval(() => {
  if (remoteExamActive && !examActive && !sessionToken) {
    tryDesktopPairing();
  }
}, DESKTOP_PAIRING_POLL_MS);

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
  await retryTabOperation(
    () => chrome.tabs.update(tabId, { url: target }),
    'Sekme sınav sayfasına yönlendirilemedi'
  );
}

async function retryTabOperation(operation, label, attempts = 3) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await operation();
    } catch (error) {
      const retryable = String(error?.message || '').includes('Tabs cannot be edited');
      if (!retryable || attempt === attempts) {
        console.error(`[ExamGuard] ${label}:`, error);
        return null;
      }
      await new Promise(resolve => setTimeout(resolve, 250 * attempt));
    }
  }
  return null;
}

async function captureUnfocusEvidence(details = '') {
  const now = Date.now();
  if (now - lastUnfocusCaptureAt < UNFOCUS_CAPTURE_COOLDOWN_MS) return;
  lastUnfocusCaptureAt = now;
  await captureAndSend('window_unfocused', details || 'Chrome arka plana atıldı');
}

function clearPageUnfocusTimer(tabId) {
  const timer = pageUnfocusTimers.get(tabId);
  if (timer) clearTimeout(timer);
  pageUnfocusTimers.delete(tabId);
}

function clearPageUnfocusTimers() {
  for (const timer of pageUnfocusTimers.values()) clearTimeout(timer);
  pageUnfocusTimers.clear();
}

function schedulePageUnfocusCapture(tabId) {
  clearPageUnfocusTimer(tabId);
  const timer = setTimeout(async () => {
    pageUnfocusTimers.delete(tabId);
    if (!examActive) return;
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.active) return;
      await captureUnfocusEvidence(
        'Sekme en az 3 saniye boyunca arka planda kaldı'
      );
    } catch {
      // Sekme kapatıldıysa kanıt üretme.
    }
  }, UNFOCUS_CAPTURE_DELAY_MS);
  pageUnfocusTimers.set(tabId, timer);
}

function clearWindowUnfocusTimer() {
  if (windowUnfocusTimer) clearTimeout(windowUnfocusTimer);
  windowUnfocusTimer = null;
}

function scheduleWindowUnfocusCapture() {
  clearWindowUnfocusTimer();
  windowUnfocusTimer = setTimeout(async () => {
    windowUnfocusTimer = null;
    if (!examActive) return;
    const lastFocusedWindow = await chrome.windows.getLastFocused().catch(() => null);
    if (lastFocusedWindow?.focused) return;
    await captureUnfocusEvidence(
      'Chrome en az 3 saniye boyunca arka planda kaldı veya alta indirildi'
    );
  }, UNFOCUS_CAPTURE_DELAY_MS);
}

// ─────────────────────────────────────────
// YENİ SEKME AÇMA
// ─────────────────────────────────────────
chrome.tabs.onCreated.addListener(async (tab) => {
  if (!examActive) return;
  await captureAndSend('new_tab_attempt', tab.pendingUrl || tab.url || 'yeni sekme');
  setTimeout(() => {
    retryTabOperation(
      () => chrome.tabs.remove(tab.id),
      'Yeni sekme kapatılamadı'
    );
  }, 300);
});

// ─────────────────────────────────────────
// YENİ PENCERE AÇMA
// ─────────────────────────────────────────
chrome.windows.onCreated.addListener(async (win) => {
  if (!examActive) return;
  // Extension popup pencerelerini atla
  if (win.type === 'popup') return;
  await captureAndSend('new_tab_attempt', 'Yeni pencere açma teşebbüsü');
  setTimeout(() => chrome.windows.remove(win.id).catch(() => {}), 300);
});

// ─────────────────────────────────────────
// CHROME ARKA PLANA ATILDI
// ─────────────────────────────────────────
chrome.windows.onFocusChanged.addListener(async (windowId) => {
  if (!examActive) return;
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    scheduleWindowUnfocusCapture();
    return;
  }
  clearWindowUnfocusTimer();
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
    return current.hostname === 'moodle.erzurum.edu.tr';
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
function isCapturableTab(tab) {
  return (
    tab &&
    Number.isInteger(tab.id) &&
    Number.isInteger(tab.windowId) &&
    /^https?:\/\//i.test(tab.url || '')
  );
}

async function findCapturableActiveTab() {
  const windows = await chrome.windows.getAll({
    populate: true,
    windowTypes: ['normal']
  });
  const ordered = windows.sort((a, b) => Number(b.focused) - Number(a.focused));
  for (const window of ordered) {
    const activeTab = (window.tabs || []).find(tab => tab.active && isCapturableTab(tab));
    if (activeTab) return activeTab;
  }
  const tabs = await chrome.tabs.query({});
  return tabs.find(tab => tab.active && isCapturableTab(tab)) || null;
}

async function captureTabWithRetry(windowId, attempts = 3) {
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await chrome.tabs.captureVisibleTab(windowId, {
        format: 'jpeg',
        quality: 75
      });
    } catch (error) {
      lastError = error;
      const message = String(error?.message || '');
      const retryable = (
        message.includes('Tabs cannot be edited') ||
        message.includes('activeTab permission is not in effect')
      );
      if (!retryable || attempt === attempts) break;
      await new Promise(resolve => setTimeout(resolve, 300 * attempt));
    }
  }
  throw lastError || new Error('Ekran görüntüsü alınamadı.');
}

async function captureAndSend(reason, details = '') {
  try {
    const activeTab = await findCapturableActiveTab();
    if (!activeTab) {
      await sendCaptureFailureAlert(
        reason,
        details,
        null,
        new Error('Erişilebilir HTTP/HTTPS sekmesi bulunamadı.')
      );
      return;
    }

    let screenshot;
    try {
      screenshot = await captureTabWithRetry(activeTab.windowId);
      lastSuccessfulScreenshot = screenshot;
    } catch (captureError) {
      if (reason === 'window_unfocused' && lastSuccessfulScreenshot) {
        screenshot = lastSuccessfulScreenshot;
        details = [
          details,
          'Chrome küçültüldüğü için son başarılı sekme görüntüsü kullanıldı.'
        ].filter(Boolean).join(' ');
      } else {
        await sendCaptureFailureAlert(reason, details, activeTab, captureError);
        return;
      }
    }

    const response = await fetch(`${BACKEND_URL}/screenshot`, {
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
    if (!response.ok) {
      const body = await response.text();
      if (response.status === 401) await clearLocalSession();
      throw new Error(`Screenshot HTTP ${response.status}: ${body}`);
    }
  } catch (err) {
    console.error('[ExamGuard] Capture error:', err);
  }
}

async function sendCaptureFailureAlert(reason, details, activeTab, error) {
  if (!sessionToken || !studentInfo) return;
  try {
    const response = await fetch(`${BACKEND_URL}/alert`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({
        type: reason,
        reason,
        details: [
          details,
          `Ekran görüntüsü alınamadı: ${error?.message || 'bilinmeyen hata'}`
        ].filter(Boolean).join(' '),
        student: studentInfo,
        timestamp: new Date().toISOString(),
        tabUrl: activeTab?.url || '',
        tabTitle: activeTab?.title || ''
      })
    });
    if (response.status === 401) await clearLocalSession();
    if (!response.ok) throw new Error(`Alert HTTP ${response.status}`);
  } catch (alertError) {
    console.error('[ExamGuard] Capture failure alert error:', alertError);
  }
}

// ─────────────────────────────────────────
// ÖĞRENCİ KATILIM / AYRILMA / HEARTBEAT
// ─────────────────────────────────────────
async function studentJoin(student) {
  try {
    const response = await fetch(`${BACKEND_URL}/student/join`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({ student, timestamp: new Date().toISOString() })
    });
    if (response.status === 401) await clearLocalSession();
    if (!response.ok) throw new Error(`Join HTTP ${response.status}`);
    return true;
  } catch (e) {
    console.error('[ExamGuard] Join error:', e);
    return false;
  }
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
    const response = await fetch(`${BACKEND_URL}/student/heartbeat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${sessionToken}`
      },
      body: JSON.stringify({ student: studentInfo, timestamp: new Date().toISOString() })
    });
    if (response.status === 401) await clearLocalSession();
    if (!response.ok) throw new Error(`Heartbeat HTTP ${response.status}`);
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
