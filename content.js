// ── BMS Seat Alert — Content Script (Sniper Mode) ─────────────────────────────
// Injected into every in.bookmyshow.com page.
// On seat-layout pages: reads show info, injects the Sniper widget.
// On other BMS pages: stays invisible, answers GET_PAGE_INFO from popup.

const SERVER_URL = "https://bms-alert-app-production.up.railway.app";
const WIDGET_ID  = "bms-seat-alert-widget";

// ── Parse show info from current URL + DOM ─────────────────────────────────────
function parseShowInfo() {
  // URL format: /movies/{city}/seat-layout/{eventCode}/{venueCode}/{showId}/{date}
  const m = window.location.pathname.match(
    /\/seat-layout\/([^/]+)\/([^/]+)\/([^/]+)\/(\d{8})/
  );
  if (!m) return null;

  const [, eventCode, venueCode, showId, date] = m;

  // Human-readable info from DOM
  const movieName = _readMovieName();
  const venueName = _readVenueName();
  const showtime  = _readShowtime();

  // Format date for display: 20260321 → 21 Mar 2026
  const d = date;
  const dateStr = `${d.slice(6,8)} ${['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(d.slice(4,6))]} ${d.slice(0,4)}`;

  return { eventCode, venueCode, showId, date, movieName, venueName, showtime, dateStr };
}

function _readMovieName() {
  // BMS title: "Youth - Book Tickets Online | BookMyShow" or page H1/H2
  const selectors = ['h1', 'h2', '[class*="movie-title"]', '[class*="movieTitle"]'];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim()) return el.textContent.trim().split(/[-|]/)[0].trim();
  }
  return document.title.replace(/[-|].*/, '').replace(/BookMyShow/i, '').trim() || 'This show';
}

function _readVenueName() {
  // BMS shows venue in breadcrumb / header detail line
  const selectors = [
    '[class*="venue"]', '[class*="Venue"]',
    '[class*="theatre"]', '[class*="Theatre"]',
    '[class*="cinema"]', '[class*="Cinema"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim().length > 2) return el.textContent.trim();
  }
  // Fallback: read from the detail bar text
  const detail = document.querySelector('[class*="detail"], [class*="Detail"], [class*="info"]');
  if (detail) {
    const text = detail.textContent.trim();
    const parts = text.split(/\|/);
    if (parts.length > 0) return parts[0].trim();
  }
  return '';
}

function _readShowtime() {
  // Active showtime button (highlighted) or from detail bar
  const activeTime = document.querySelector(
    '[class*="showtime"][class*="active"], [class*="ShowTime"][class*="active"], ' +
    '[class*="time-slot"][class*="active"], button[class*="active"][class*="show"]'
  );
  if (activeTime?.textContent?.trim()) return activeTime.textContent.trim();

  // Try reading from the detail/subheader bar
  const detail = document.querySelector('[class*="detail"], [class*="Detail"]');
  if (detail) {
    const text = detail.textContent;
    const timeMatch = text.match(/\d{1,2}:\d{2}\s*(AM|PM)/i);
    if (timeMatch) return timeMatch[0];
  }
  return '';
}

// ── Detect if current show is sold out ────────────────────────────────────────
function detectSoldOut() {
  const body = document.body.innerText.toLowerCase();

  // Strong sold-out signals
  const soldPhrases = ['sold out', 'housefull', 'no seats available', 'sold-out'];
  for (const p of soldPhrases) {
    if (body.includes(p)) return { soldOut: true, reason: p };
  }

  // Check if seat map has NO available seats (all grey/sold)
  const allSeats   = document.querySelectorAll('[class*="seat"], [class*="Seat"]').length;
  const soldSeats  = document.querySelectorAll('[class*="sold"], [class*="Sold"], [class*="unavailable"]').length;
  if (allSeats > 0 && soldSeats > 0 && soldSeats >= allSeats * 0.98) {
    return { soldOut: true, reason: 'all seats sold' };
  }

  return { soldOut: false, reason: '' };
}

// ── Widget HTML ───────────────────────────────────────────────────────────────
function buildWidgetHTML(show) {
  return `
<div id="${WIDGET_ID}" style="
  position: fixed;
  top: 80px;
  right: 16px;
  width: 224px;
  z-index: 999999;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
">
  <style>
    #bsa-cta, #bsa-monitoring, #bsa-found { display: none; }
    #bsa-cta.bsa-active, #bsa-monitoring.bsa-active, #bsa-found.bsa-active { display: block; }

    .bsa-card {
      background: #1a1a2e;
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 12px 40px rgba(0,0,0,0.55);
      border: 1px solid rgba(255,255,255,0.09);
    }
    .bsa-header {
      background: linear-gradient(135deg, #7f1d1d, #dc2626);
      padding: 11px 13px;
      display: flex;
      align-items: center;
      gap: 7px;
    }
    .bsa-header-found {
      background: linear-gradient(135deg, #14532d, #166534);
    }
    .bsa-header-dot {
      width: 7px; height: 7px;
      border-radius: 50%; background: rgba(255,255,255,0.8);
      animation: bsa-blink 1.5s ease-in-out infinite;
    }
    @keyframes bsa-blink { 0%,100%{opacity:1} 50%{opacity:0.2} }
    .bsa-header-title {
      color: #fff; font-size: 12px; font-weight: 800;
    }
    .bsa-body { padding: 12px 13px; }

    .bsa-show-pill {
      background: rgba(255,255,255,0.05);
      border-left: 2px solid rgba(220,38,38,0.6);
      border-radius: 0 7px 7px 0;
      padding: 8px 10px;
      margin-bottom: 10px;
    }
    .bsa-show-pill.bsa-green-border { border-left-color: #22c55e; }
    .bsa-show-name   { color: rgba(255,255,255,0.85); font-size: 12px; font-weight: 600; }
    .bsa-show-detail { color: rgba(255,255,255,0.35); font-size: 10px; margin-top: 2px; }

    .bsa-prompt { color: rgba(255,255,255,0.4); font-size: 11px; text-align: center; margin-bottom: 10px; }

    .bsa-btn-watch {
      width: 100%; padding: 12px 8px;
      background: linear-gradient(135deg, #dc2626, #ef4444);
      border: none; border-radius: 9px;
      color: #fff; font-size: 13px; font-weight: 800;
      cursor: pointer; letter-spacing: -0.2px;
      box-shadow: 0 5px 16px rgba(220,38,38,0.4);
      transition: opacity 0.15s;
    }
    .bsa-btn-watch:hover { opacity: 0.88; }

    .bsa-cust-toggle {
      width: 100%; background: none; border: none;
      color: rgba(255,255,255,0.25); font-size: 10px;
      cursor: pointer; padding: 7px 0 0; text-align: center;
      transition: color 0.15s;
    }
    .bsa-cust-toggle:hover { color: rgba(255,255,255,0.5); }

    .bsa-cust-panel { display: none; padding-top: 8px; }
    .bsa-cust-panel.bsa-open { display: block; }
    .bsa-cust-label { color: rgba(255,255,255,0.25); font-size: 10px; margin-bottom: 6px; }
    .bsa-pref-row { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 8px; }
    .bsa-back-row-wrap {
      display: flex; align-items: flex-start; gap: 8px;
      cursor: pointer; padding: 8px 0;
    }
    .bsa-back-row-wrap input[type="checkbox"] {
      width: 15px; height: 15px; margin-top: 1px;
      accent-color: #dc2626; cursor: pointer; flex-shrink: 0;
    }
    .bsa-back-row-label {
      color: rgba(255,255,255,0.75); font-size: 12px; font-weight: 600;
      display: block; line-height: 1.3;
    }
    .bsa-back-row-hint {
      color: rgba(255,255,255,0.25); font-size: 10px;
      display: block; margin-top: 1px;
    }

    .bsa-phone-wrap { display: flex; gap: 5px; align-items: center; }
    .bsa-phone-pre {
      background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
      border-radius: 6px; padding: 7px 8px;
      color: rgba(255,255,255,0.4); font-size: 10px; white-space: nowrap;
    }
    .bsa-phone-input {
      flex: 1; background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 6px; padding: 7px 8px;
      color: #fff; font-size: 11px; outline: none;
    }
    .bsa-phone-input:focus { border-color: rgba(220,38,38,0.5); }
    .bsa-phone-input::placeholder { color: rgba(255,255,255,0.2); }

    .bsa-status-row {
      display: flex; align-items: center; gap: 6px;
      margin-bottom: 10px;
    }
    .bsa-status-dot {
      width: 6px; height: 6px; border-radius: 50%; background: #22c55e;
      animation: bsa-blink 1.8s infinite; flex-shrink: 0;
    }
    .bsa-status-text { color: rgba(255,255,255,0.4); font-size: 11px; }

    .bsa-btn-stop {
      width: 100%; padding: 9px; border-radius: 8px;
      border: 1px solid rgba(239,68,68,0.2);
      background: rgba(239,68,68,0.06);
      color: rgba(239,68,68,0.6); font-size: 11px; font-weight: 600;
      cursor: pointer;
    }
    .bsa-btn-stop:hover { background: rgba(239,68,68,0.12); }

    /* Found */
    .bsa-found-emoji { font-size: 28px; text-align: center; margin-bottom: 6px; }
    .bsa-found-title { color: #fff; font-size: 17px; font-weight: 800; text-align: center; margin-bottom: 3px; }
    .bsa-found-where { color: rgba(255,255,255,0.6); font-size: 11px; text-align: center; margin-bottom: 10px; }
    .bsa-btn-book {
      width: 100%; padding: 13px; border-radius: 10px; border: none;
      background: #22c55e; color: #fff; font-size: 13px; font-weight: 800;
      cursor: pointer; box-shadow: 0 5px 16px rgba(34,197,94,0.4);
    }
    .bsa-btn-book:hover { opacity: 0.9; }
    .bsa-btn-dismiss {
      width: 100%; background: none; border: none;
      color: rgba(255,255,255,0.2); font-size: 10px;
      cursor: pointer; padding: 7px 0 0; text-align: center;
    }

    /* Minimise toggle */
    .bsa-minimise {
      position: absolute; top: 11px; right: 11px;
      background: rgba(255,255,255,0.1); border: none;
      border-radius: 50%; width: 18px; height: 18px;
      color: rgba(255,255,255,0.5); font-size: 10px;
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      line-height: 1;
    }
    .bsa-minimise:hover { background: rgba(255,255,255,0.2); }
  </style>

  <!-- State A: CTA -->
  <div id="bsa-cta" class="bsa-card bsa-active">
    <div class="bsa-header" style="position:relative;">
      <div class="bsa-header-title">🎬 Seat Alert</div>
      <button class="bsa-minimise" id="bsa-minimise-btn">−</button>
    </div>
    <div class="bsa-body">
      <div class="bsa-show-pill">
        <div class="bsa-show-name">${show.movieName || 'This show'}</div>
        <div class="bsa-show-detail">${show.venueName ? show.venueName + ' · ' : ''}${show.showtime || ''}</div>
      </div>
      <div class="bsa-prompt">Seats sold out? I'll alert you the moment one opens.</div>
      <button class="bsa-btn-watch" id="bsa-watch-btn">Watch this show →</button>
      <button class="bsa-cust-toggle" id="bsa-cust-toggle">⚙ Customize ▾</button>
      <div class="bsa-cust-panel" id="bsa-cust-panel">
        <div class="bsa-cust-label">Seat preference</div>
        <label class="bsa-back-row-wrap">
          <input type="checkbox" id="bsa-back-only" />
          <span class="bsa-back-row-label">Back rows only</span>
          <span class="bsa-back-row-hint">Better view, may wait longer</span>
        </label>
        <div class="bsa-cust-label">WhatsApp</div>
        <div class="bsa-phone-wrap">
          <span class="bsa-phone-pre">🇮🇳 +91</span>
          <input class="bsa-phone-input" id="bsa-phone" type="tel" placeholder="98765 43210" maxlength="10" />
        </div>
      </div>
    </div>
  </div>

  <!-- State B: Monitoring -->
  <div id="bsa-monitoring" class="bsa-card">
    <div class="bsa-header">
      <div class="bsa-header-dot"></div>
      <div class="bsa-header-title">Watching this show</div>
    </div>
    <div class="bsa-body">
      <div class="bsa-show-pill bsa-green-border">
        <div class="bsa-show-name">${show.movieName || 'This show'}</div>
        <div class="bsa-show-detail" id="bsa-mon-detail">${show.venueName ? show.venueName + ' · ' : ''}${show.showtime || ''}</div>
      </div>
      <div class="bsa-status-row">
        <div class="bsa-status-dot"></div>
        <div class="bsa-status-text" id="bsa-status-text">Checking for cancellations…</div>
      </div>
      <div class="bsa-status-row" style="margin-bottom:10px;">
        <div style="width:6px;flex-shrink:0;"></div>
        <div class="bsa-status-text" id="bsa-pref-text" style="color:rgba(255,255,255,0.2);">Preference: Best available</div>
      </div>
      <button class="bsa-btn-stop" id="bsa-stop-btn">Stop watching</button>
    </div>
  </div>

  <!-- State C: Found -->
  <div id="bsa-found" class="bsa-card">
    <div class="bsa-header bsa-header-found">
      <div class="bsa-header-title" style="width:100%;text-align:center;">🎉 Seats available!</div>
    </div>
    <div class="bsa-body">
      <div class="bsa-found-where" id="bsa-found-where">${show.movieName} · ${show.showtime}</div>
      <button class="bsa-btn-book" id="bsa-book-btn">Book now →</button>
      <button class="bsa-btn-dismiss" id="bsa-dismiss-btn">Dismiss</button>
    </div>
  </div>

</div>`;
}

// ── Widget state machine ───────────────────────────────────────────────────────
let _widgetState  = 'cta';    // 'cta' | 'monitoring' | 'found' | 'hidden'
let _monitorId    = null;
let _pollInterval = null;
let _selectedPref = 'best';
let _show         = null;

function _setWidgetState(state) {
  _widgetState = state;
  const cta = document.getElementById('bsa-cta');
  const mon = document.getElementById('bsa-monitoring');
  const fnd = document.getElementById('bsa-found');
  if (!cta) return;
  cta.classList.toggle('bsa-active', state === 'cta');
  mon.classList.toggle('bsa-active', state === 'monitoring');
  fnd.classList.toggle('bsa-active', state === 'found');
}

function _attachWidgetListeners(show) {
  // Back rows checkbox
  const backOnlyCheckbox = document.getElementById('bsa-back-only');
  if (backOnlyCheckbox) {
    backOnlyCheckbox.addEventListener('change', () => {
      _selectedPref = backOnlyCheckbox.checked ? 'back' : 'best';
    });
  }

  // Customize toggle
  document.getElementById('bsa-cust-toggle').addEventListener('click', () => {
    const panel = document.getElementById('bsa-cust-panel');
    const isOpen = panel.classList.toggle('bsa-open');
    document.getElementById('bsa-cust-toggle').textContent = isOpen ? '⚙ Customize ▴' : '⚙ Customize ▾';
  });

  // Minimise
  document.getElementById('bsa-minimise-btn').addEventListener('click', () => {
    const widget = document.getElementById(WIDGET_ID);
    if (widget) widget.style.display = 'none';
  });

  // Watch this show
  document.getElementById('bsa-watch-btn').addEventListener('click', async () => {
    // Read phone from customize panel (or from chrome.storage)
    const phoneInput = document.getElementById('bsa-phone');
    let phone = phoneInput?.value?.replace(/\s/g, '') || '';

    // Try stored phone first
    if (!phone) {
      phone = await _getStoredPhone();
    }

    if (!phone || phone.length < 10) {
      // Open customize to enter phone
      const panel = document.getElementById('bsa-cust-panel');
      panel.classList.add('bsa-open');
      document.getElementById('bsa-cust-toggle').textContent = '⚙ Customize ▴';
      phoneInput?.focus();
      return;
    }

    await _startSniperMonitor(show, phone);
  });

  // Stop
  document.getElementById('bsa-stop-btn').addEventListener('click', async () => {
    clearInterval(_pollInterval);
    if (_monitorId) {
      try { await fetch(`${SERVER_URL}/api/monitor/${_monitorId}/stop`, { method: 'POST' }); } catch (_) {}
    }
    _monitorId = null;
    chrome.storage.local.set({ sniperJob: null });
    _setWidgetState('cta');
  });

  // Book now
  document.getElementById('bsa-book-btn').addEventListener('click', () => {
    // The current URL is the seat layout — user is already here
    window.location.reload();
  });

  // Dismiss
  document.getElementById('bsa-dismiss-btn').addEventListener('click', () => {
    chrome.storage.local.set({ sniperJob: null });
    const widget = document.getElementById(WIDGET_ID);
    if (widget) widget.style.display = 'none';
  });
}

// ── Start sniper monitor ───────────────────────────────────────────────────────
async function _startSniperMonitor(show, phone) {
  _setWidgetState('monitoring');
  const prefText = _selectedPref === 'back'
    ? 'Back rows only · will skip front & middle'
    : 'Best available · any open seat';
  document.getElementById('bsa-pref-text').textContent = prefText;

  const payload = {
    movie:       show.movieName,
    theatre:     show.venueName,
    showtime:    show.showtime,
    show_id:     show.showId,
    event_code:  show.eventCode,
    venue_code:  show.venueCode,
    date:        show.date,
    booking_url: window.location.href,
    zone_pref:   _selectedPref,
    phone:       '+91' + phone.replace(/\D/g, '').slice(-10),
    poll_interval: 15,
    mode:        'sniper',
  };

  // Save phone for future use
  chrome.storage.local.set({ sniperPhone: phone });

  try {
    const resp = await fetch(`${SERVER_URL}/api/monitor`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    _monitorId = data.monitor_id;

    chrome.storage.local.set({
      sniperJob: { monitorId: _monitorId, show, pref: _selectedPref }
    });

    // Start polling server for status
    _startStatusPoll();

  } catch (err) {
    _setWidgetState('cta');
    document.getElementById('bsa-status-text').textContent = 'Could not reach server';
  }
}

function _startStatusPoll() {
  clearInterval(_pollInterval);
  let pollCount = 0;
  _pollInterval = setInterval(async () => {
    if (!_monitorId) return;
    try {
      const resp = await fetch(`${SERVER_URL}/api/monitor/${_monitorId}`);
      const data = await resp.json();
      pollCount++;

      const statusEl = document.getElementById('bsa-status-text');
      if (statusEl) statusEl.textContent = `Checking… (${pollCount} scans)`;

      if (data.status === 'seats_found' || data.alert_sent) {
        clearInterval(_pollInterval);
        // Update found card
        const foundWhere = document.getElementById('bsa-found-where');
        if (foundWhere) {
          foundWhere.textContent = `${_show?.movieName || 'Show'} · ${_show?.showtime || ''} · ${_show?.venueName || ''}`;
        }
        _setWidgetState('found');
        chrome.storage.local.set({ sniperJob: null });
      }
    } catch (_) {}
  }, 8000);
}

// ── Inject widget ──────────────────────────────────────────────────────────────
function injectWidget(show) {
  // Remove existing widget if any
  const existing = document.getElementById(WIDGET_ID);
  if (existing) existing.remove();

  const div = document.createElement('div');
  div.innerHTML = buildWidgetHTML(show);
  document.body.appendChild(div.firstElementChild);
  _attachWidgetListeners(show);
}

// ── URL change detection (BMS uses client-side routing) ───────────────────────
let _lastUrl = window.location.href;

function _onUrlChange() {
  const current = window.location.href;
  if (current === _lastUrl) return;
  _lastUrl = current;

  // Slight delay to let DOM settle after navigation
  setTimeout(() => {
    const show = parseShowInfo();
    if (show) {
      _show = show;
      const existing = document.getElementById(WIDGET_ID);
      if (existing) {
        // Update show info in place without rebuilding (preserves monitoring state)
        const nameEls = existing.querySelectorAll('.bsa-show-name');
        nameEls.forEach(el => { el.textContent = show.movieName || 'This show'; });
        const detailEls = existing.querySelectorAll('.bsa-show-detail');
        detailEls.forEach(el => {
          el.textContent = (show.venueName ? show.venueName + ' · ' : '') + (show.showtime || '');
        });
      } else {
        injectWidget(show);
      }
    } else {
      // Left the seat layout page — hide widget
      const w = document.getElementById(WIDGET_ID);
      if (w) w.style.display = 'none';
    }
  }, 800);
}

// Watch for pushState / replaceState navigation
const _origPush    = history.pushState.bind(history);
const _origReplace = history.replaceState.bind(history);
history.pushState    = (...a) => { _origPush(...a);    _onUrlChange(); };
history.replaceState = (...a) => { _origReplace(...a); _onUrlChange(); };
window.addEventListener('popstate', _onUrlChange);

// MutationObserver as fallback for any DOM-driven URL changes
const _urlObserver = new MutationObserver(_onUrlChange);
_urlObserver.observe(document.body, { childList: true, subtree: true });

// ── Helpers ───────────────────────────────────────────────────────────────────
function _prefLabel(p) {
  return { best: 'Best available', back: 'Back rows', mid: 'Middle', nfront: 'Avoid front' }[p] || p;
}

async function _getStoredPhone() {
  return new Promise(resolve => {
    chrome.storage.local.get('sniperPhone', d => resolve(d.sniperPhone || ''));
  });
}

// ── Message handler (popup still calls GET_PAGE_INFO) ─────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'GET_PAGE_INFO') {
    const show = parseShowInfo();
    sendResponse({
      showName: show?.movieName || document.title.replace(/[-|].*/, '').trim(),
      url: window.location.href,
      showInfo: show,
    });
  }
  return true;
});

// ── Boot ───────────────────────────────────────────────────────────────────────
(function boot() {
  const show = parseShowInfo();
  if (!show) return; // Not on a seat-layout page

  _show = show;

  // Check if there's an active sniper job for this show
  chrome.storage.local.get('sniperJob', data => {
    if (data.sniperJob?.show?.showId === show.showId) {
      // Resume monitoring state
      _monitorId = data.sniperJob.monitorId;
      _selectedPref = data.sniperJob.pref || 'best';
      injectWidget(show);
      _setWidgetState('monitoring');
      _startStatusPoll();
    } else {
      // Fresh — show CTA only if page appears sold out
      injectWidget(show);
      const { soldOut } = detectSoldOut();
      if (!soldOut) {
        // Seats are available — hide widget (nothing to do)
        const w = document.getElementById(WIDGET_ID);
        if (w) w.style.display = 'none';
      }
      // If sold out → widget stays visible with CTA
    }
  });
})();
