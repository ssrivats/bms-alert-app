// ── BMS Seat Alert — Popup (Hybrid Mode) ──────────────────────────────────────
// Extension collects info from the BMS page → sends job to Railway server
// Server monitors 24/7 and sends WhatsApp when seats open

const ROWS = 'A B C D E F G H I J K L M N O P'.split(' ');
let selectedRows = [];
let currentTab  = null;
const serverUrl = "https://bms-alert-app-production.up.railway.app";

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab;

  const isBms = tab?.url?.includes('in.bookmyshow.com');
  if (!isBms) { show('notBmsView'); return; }

  // Load saved state
  chrome.storage.local.get(['activeJob', 'alertSent'], (data) => {

    // Alert already sent for this job
    if (data.alertSent) { showSuccessView(data.activeJob); return; }

    // Job running on server
    if (data.activeJob) { showMonitorView(data.activeJob); return; }

    // Normal setup
    show('setupView');
    initSetupView();
  });
});

// ── ① Setup view ──────────────────────────────────────────────────────────────
function initSetupView() {
  buildRowGrid();

  // Detect show name from the tab
  chrome.tabs.sendMessage(currentTab.id, { type: 'GET_PAGE_INFO' }, (resp) => {
    void chrome.runtime.lastError;
    const name = resp?.showName
      || currentTab.title?.replace(/ [-|] BookMyShow.*/i, '').trim()
      || 'Your show';
    document.getElementById('showNameLabel').textContent = name;
  });

  document.getElementById('phoneInput').addEventListener('input', updateNotifyBtn);
  document.getElementById('notifyBtn').addEventListener('click', submitJob, { once: true });
}

function buildRowGrid() {
  const grid = document.getElementById('rowGrid');
  grid.innerHTML = ROWS.map(r =>
    `<button class="row-btn" data-row="${r}">${r}</button>`
  ).join('');
  grid.querySelectorAll('.row-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.dataset.row;
      if (selectedRows.includes(row)) {
        selectedRows = selectedRows.filter(r => r !== row);
        btn.classList.remove('selected');
      } else {
        selectedRows.push(row);
        btn.classList.add('selected');
      }
      document.getElementById('rowHint').textContent = selectedRows.length > 0
        ? `Alerting for row${selectedRows.length > 1 ? 's' : ''}: ${selectedRows.join(', ')}`
        : 'No row selected — alerts for any seat';
      updateNotifyBtn();
    });
  });
}

function updateNotifyBtn() {
  const phone = document.getElementById('phoneInput').value.replace(/\s/g, '');
  document.getElementById('notifyBtn').disabled = phone.length < 10;
}

// ── ③ Submit job to Railway server ────────────────────────────────────────────
async function submitJob() {
  const phone    = '+91' + document.getElementById('phoneInput').value.replace(/\s/g, '');
  const showName = document.getElementById('showNameLabel').textContent;

  show('submittingView');

  const job = {
    movie:         showName,
    theatre:       '',
    showtime:      '',
    booking_url:   currentTab.url,
    show_id:       '',
    preferred_row: selectedRows.join(','),
    preferred_seats: '',
    phone,
    poll_interval: 8,
    city:          '',
  };

  try {
    const resp = await fetch(`${serverUrl}/api/monitor`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(job),
    });

    if (!resp.ok) throw new Error(`Server error: ${resp.status}`);

    const data = await resp.json();
    const activeJob = { ...job, monitorId: data.monitor_id };
    chrome.storage.local.set({ activeJob, alertSent: false });
    showMonitorView(activeJob);

  } catch (err) {
    show('setupView');
    initSetupView();
    // Re-select the rows that were chosen
    selectedRows.forEach(r => {
      document.querySelector(`[data-row="${r}"]`)?.classList.add('selected');
    });
    alert(`Could not reach server. Please try again.\n\nError: ${err.message}`);
    // Re-attach submit listener
    document.getElementById('notifyBtn').addEventListener('click', submitJob, { once: true });
  }
}

// ── ④ Monitor view ────────────────────────────────────────────────────────────
let statusPollTimer = null;

function showMonitorView(job) {
  show('monitorView');
  document.getElementById('monShowName').textContent = job.movie || 'Your show';
  document.getElementById('monRows').textContent     = job.preferred_row || 'Any seat';
  document.getElementById('monPhone').textContent    = job.phone || '—';

  // Links to per-job log and master dashboard
  if (job.monitorId) {
    document.getElementById('logLink').href = `${serverUrl}/status/${job.monitorId}`;
  }
  document.getElementById('dashboardLink').href = `${serverUrl}/dashboard`;

  document.getElementById('cancelBtn').addEventListener('click', cancelJob, { once: true });

  // Start live status polling
  if (job.monitorId) {
    fetchStatus(job.monitorId);
    statusPollTimer = setInterval(() => fetchStatus(job.monitorId), 5000);
  }
}

async function fetchStatus(monitorId) {
  try {
    const resp = await fetch(`${serverUrl}/api/monitor/${monitorId}`);
    if (!resp.ok) return;
    const data = await resp.json();

    // Update live fields
    document.getElementById('monPolls').textContent      = data.poll_count ?? '—';
    document.getElementById('monLastChecked').textContent = data.last_checked ?? '—';
    document.getElementById('monLastResult').textContent  = data.last_result  ?? '—';

    // Status pill
    const pill = document.querySelector('.status-pill');
    const txt  = document.getElementById('statusText');
    if (data.status === 'seats_found' || data.alert_sent) {
      clearInterval(statusPollTimer);
      pill.style.background = 'rgba(52,211,153,0.12)';
      pill.style.color      = '#34d399';
      txt.textContent       = '🎉 Seats found! WhatsApp sent.';
      chrome.storage.local.set({ alertSent: true });
    } else if (data.status === 'stopped') {
      clearInterval(statusPollTimer);
      pill.style.background = 'rgba(239,68,68,0.1)';
      pill.style.color      = '#ef4444';
      txt.textContent       = 'Monitoring stopped';
    } else {
      txt.textContent = `Server is watching 24/7`;
    }
  } catch (_) {
    document.getElementById('monLastResult').textContent = 'Could not reach server';
  }
}

async function cancelJob() {
  clearInterval(statusPollTimer);
  chrome.storage.local.get('activeJob', async (data) => {
    const job = data.activeJob;
    if (job?.monitorId) {
      try {
        await fetch(`${serverUrl}/api/monitor/${job.monitorId}/stop`, { method: 'POST' });
      } catch (_) {}
    }
    chrome.storage.local.set({ activeJob: null, alertSent: false });
    selectedRows = [];
    show('setupView');
    initSetupView();
  });
}

// ── ⑤ Success view ────────────────────────────────────────────────────────────
function showSuccessView(job) {
  show('successView');
  document.getElementById('successMsg').textContent =
    `WhatsApp sent to ${job?.phone || 'your number'}. Booking link included.`;

  document.getElementById('watchAnotherBtn').addEventListener('click', () => {
    chrome.storage.local.set({ activeJob: null, alertSent: false });
    selectedRows = [];
    show('setupView');
    initSetupView();
  }, { once: true });
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function show(viewId) {
  document.querySelectorAll('.view').forEach(v => {
    v.classList.toggle('active', v.id === viewId);
  });
}
