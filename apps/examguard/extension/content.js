// ExamGuard - content.js
// Moodle sayfasına inject olur, kopya/yapıştır ve kısayolları engeller

(function () {
  'use strict';

  // Sadece aktif sınav varsa çalış
  chrome.storage.local.get(['examActive'], ({ examActive }) => {
    if (!examActive) return;
    initGuard();
  });

  function initGuard() {
    // ── Sağ tık menüsünü engelle ──
    document.addEventListener('contextmenu', block, true);

    // ── Kopyala / Kes / Yapıştır engelle ──
    ['copy', 'cut', 'paste'].forEach(ev =>
      document.addEventListener(ev, block, true)
    );

    // ── Klavye kısayolları engelle ──
    document.addEventListener('keydown', (e) => {
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
      // Input ve textarea içinde izin ver
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
      // Sınav cevap alanlarında izin ver
      if (e.target.closest('.qtype_essay_response, .answer, .formulation')) return;
      e.preventDefault();
    }, true);

    // ── Sayfa görünürlük değişimi ──
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        chrome.runtime.sendMessage({ type: 'PAGE_HIDDEN' });
      }
    });

    console.log('[ExamGuard] Aktif — bu sayfa korunuyor');
    showBadge();
  }

  function block(e) {
    e.preventDefault();
    e.stopPropagation();
    return false;
  }

  // Sağ alt köşede küçük bir rozet göster
  function showBadge() {
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
