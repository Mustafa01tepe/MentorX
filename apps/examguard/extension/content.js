// ExamGuard - content.js
// Moodle sayfasına inject olur, kopya/yapıştır ve kısayolları engeller

(function () {
  'use strict';

  if (globalThis.__examGuardContentLoaded) return;
  globalThis.__examGuardContentLoaded = true;

  chrome.storage.local.get(['examActive'], ({ examActive }) => {
    if (examActive) initGuard();
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'local' || !changes.examActive) return;
    if (changes.examActive.newValue === true) initGuard();
    if (changes.examActive.newValue === false) deactivateGuard();
  });

  function initGuard() {
    globalThis.__examGuardActive = true;
    if (globalThis.__examGuardInitialized) {
      showBadge();
      return;
    }
    globalThis.__examGuardInitialized = true;
    // ── Sağ tık menüsünü engelle ──
    document.addEventListener('contextmenu', block, true);

    // ── Kopyala / Kes / Yapıştır engelle ──
    ['copy', 'cut', 'paste'].forEach(ev =>
      document.addEventListener(ev, block, true)
    );

    // ── Klavye kısayolları engelle ──
    document.addEventListener('keydown', (e) => {
      if (!globalThis.__examGuardActive) return;
      const ctrl = e.ctrlKey || e.metaKey;

      // Ctrl+C / X / V / A / U / S / P
      if (ctrl && 'cxvasupp'.includes(e.key.toLowerCase())) {
        e.preventDefault(); e.stopPropagation(); return;
      }

      // F12 — DevTools
      if (e.key === 'F12') {
        e.preventDefault(); e.stopPropagation(); return;
      }

      // Ctrl+Shift+I / J / C — DevTools
      if (ctrl && e.shiftKey && 'ijc'.includes(e.key.toLowerCase())) {
        e.preventDefault(); e.stopPropagation(); return;
      }

      // Alt+F4
      if (e.altKey && e.key === 'F4') {
        e.preventDefault(); return;
      }

    }, true);

    // ── Sürükle bırak engelle ──
    document.addEventListener('dragstart', block, true);
    document.addEventListener('drop', block, true);

    // ── Seçim engelle (isteğe bağlı) ──
    document.addEventListener('selectstart', (e) => {
      if (!globalThis.__examGuardActive) return;
      const target = (
        e.target &&
        typeof e.target.tagName === 'string' &&
        typeof e.target.closest === 'function'
      ) ? e.target : null;
      if (!target) return;
      // Input ve textarea içinde izin ver
      if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
      // Sınav cevap alanlarında izin ver
      if (target.closest('.qtype_essay_response, .answer, .formulation')) return;
      e.preventDefault();
    }, true);

    // ── Sayfa görünürlük değişimi ──
    document.addEventListener('visibilitychange', () => {
      if (!globalThis.__examGuardActive) return;
      chrome.runtime.sendMessage({
        type: 'PAGE_VISIBILITY_CHANGED',
        hidden: document.hidden
      });
    });

    console.log('[ExamGuard] Aktif — bu sayfa korunuyor');
    showBadge();
  }

  function deactivateGuard() {
    globalThis.__examGuardActive = false;
    document.getElementById('examguard-badge')?.remove();
  }

  function block(e) {
    if (!globalThis.__examGuardActive) return true;
    e.preventDefault();
    e.stopPropagation();
    return false;
  }

  // Sağ alt köşede küçük bir rozet göster
  function showBadge() {
    if (document.getElementById('examguard-badge')) return;
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', showBadge, { once: true });
      return;
    }
    const badge = document.createElement('div');
    badge.id = 'examguard-badge';
    badge.innerHTML = '🛡 ExamGuard Aktif';
    badge.style.cssText = `
      position: fixed;
      bottom: 12px;
      right: 12px;
      background: rgba(220, 38, 38, 0.9);
      color: white;
      font-family: monospace;
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 4px;
      z-index: 999999;
      pointer-events: none;
      letter-spacing: 0.5px;
    `;
    document.body.appendChild(badge);
  }
})();
