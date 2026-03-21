// ── BMS Seat Alert — Background (Hybrid Mode) ────────────────────────────────
// Minimal background worker — server handles all monitoring & WhatsApp alerts.
// This just keeps the extension alive and handles notification clicks if needed.

chrome.runtime.onInstalled.addListener(() => {
  console.log('BMS Seat Alert installed. Server-powered mode active.');
});
