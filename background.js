// ── BookMyShow Seat Alert — Chrome MV3 Service Worker ─────────────────────
// Owns the polling loop for all active monitors.
// Fetches BMS pages using user's cookies, parses __INITIAL_STATE__, checks availability.
// Posts updates to backend, triggers WhatsApp alerts via /api/monitor/:id/alert

const SERVER = 'https://bms-alert-app-production.up.railway.app';

// ────────────────────────────────────────────────────────────────────────────
// Utility: Fetch BMS page using user's browser context (cookies, IP)
// ────────────────────────────────────────────────────────────────────────────
async function fetchBMSPage(url) {
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
      }
    });
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    return resp.text();
  } catch (err) {
    console.error(`[fetchBMSPage] Error fetching ${url}:`, err);
    throw err;
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Extract window.__INITIAL_STATE__ JSON from HTML
// ────────────────────────────────────────────────────────────────────────────
function extractInitialState(html) {
  try {
    const marker = 'window.__INITIAL_STATE__=';
    const idx = html.indexOf(marker);
    if (idx === -1) {
      console.warn('[extractInitialState] Marker not found in HTML');
      return null;
    }
    const start = idx + marker.length;
    const endIdx = html.indexOf('</script>', start);
    if (endIdx === -1) {
      console.warn('[extractInitialState] Script end tag not found');
      return null;
    }
    const jsonStr = html.substring(start, endIdx).trim();
    const state = JSON.parse(jsonStr);
    return state;
  } catch (err) {
    console.error('[extractInitialState] Parse error:', err);
    return null;
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Normalize showtime string: trim, uppercase, fix spacing
// ────────────────────────────────────────────────────────────────────────────
function normalizeShowtime(t) {
  if (!t) return '';
  return t
    .trim()
    .toUpperCase()
    .replace(/(\d{1,2}):(\d{2})PM/i, '$1:$2 PM')
    .replace(/(\d{1,2}):(\d{2})AM/i, '$1:$2 AM');
}

// ────────────────────────────────────────────────────────────────────────────
// Extract city code from listing URL
// ────────────────────────────────────────────────────────────────────────────
function extractCityFromUrl(url) {
  try {
    const match = url.match(/\/cinemas\/([^/]+)\//);
    return match ? match[1] : 'chennai';
  } catch {
    return 'chennai';
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Build seat layout URL for session polling
// ────────────────────────────────────────────────────────────────────────────
function buildSeatLayoutUrl(monitor, sessionId) {
  const city = extractCityFromUrl(monitor.listingUrl);
  return `https://in.bookmyshow.com/movies/${city}/seat-layout/${monitor.eventCode}/${monitor.venueCode}/${sessionId}/${monitor.date}`;
}

// ────────────────────────────────────────────────────────────────────────────
// Row-level seat availability check
//
// BMS embeds seat layout data inside __INITIAL_STATE__.seatlayoutMovies.
// Somewhere inside that object is a node with a `renderGroups` array.
// Each render group contains seat objects with seatLabel (e.g. "K5", "L12").
// The row letter is the leading alpha characters of the label.
// Seat availability is indicated by status=0 or similar numeric codes.
// ────────────────────────────────────────────────────────────────────────────
function walkForRenderGroups(node, depth) {
  if (!node || depth > 12 || typeof node !== 'object') return null;
  if (Array.isArray(node.renderGroups)) return node;
  const items = Array.isArray(node) ? node : Object.values(node);
  for (const item of items) {
    if (item && typeof item === 'object') {
      const found = walkForRenderGroups(item, depth + 1);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Returns { hasData: bool, available: bool, matchedRow: string|null }
 * hasData=false means renderGroups weren't found in state — caller should not
 *   treat this as "no seats", just as "can't determine yet".
 * available=true means at least one seat in a preferred row has available status.
 */
function checkRowsInSeatLayout(seatLayoutData, preferredRows) {
  const renderData = walkForRenderGroups(seatLayoutData, 0);
  if (!renderData || !renderData.renderGroups) {
    return { hasData: false, available: false, matchedRow: null };
  }

  const wantedRows = new Set(preferredRows.map(r => String(r).toUpperCase()));

  // ── Resolve row from a seat object ─────────────────────────────────────────
  // Priority: explicit row fields first, then fall back to parsing the label.
  function resolveRow(seat) {
    const nested = seat.seat || {};
    // Direct row fields — try in order
    const direct =
      seat.rowNumber  !== undefined ? seat.rowNumber  :
      seat.rowId      !== undefined ? seat.rowId      :
      nested.rowNumber !== undefined ? nested.rowNumber :
      nested.rowId    !== undefined ? nested.rowId    :
      seat.row        !== undefined ? seat.row        :
      seat.Row        !== undefined ? seat.Row        :
      seat.rowLabel   !== undefined ? seat.rowLabel   :
      seat.RowLabel   !== undefined ? seat.RowLabel   :
      seat.seatRow    !== undefined ? seat.seatRow    :
      undefined;

    if (direct !== undefined) return String(direct).toUpperCase();

    // Last resort: parse leading alpha chars from any label field
    const label = String(
      seat.seatLabel || seat.SeatLabel ||
      nested.seatLabel || nested.SeatLabel ||
      seat.label     || seat.name      ||
      nested.label   || nested.name    || ''
    );
    const m = label.match(/^([A-Za-z]+)/);
    return m ? m[1].toUpperCase() : null;
  }

  // ── Resolve availability from a seat object ─────────────────────────────────
  // Unavailable if status resolves to 0/2/3 (numeric or string) or known strings.
  const UNAVAILABLE = new Set([0, 2, 3, '0', '2', '3', 'SOLD', 'BLOCKED', 'UNAVAILABLE']);

  function resolveAvailable(seat) {
    const nested = seat.seat || {};
    // Try status fields in priority order
    const st =
      seat.seatStatus  !== undefined ? seat.seatStatus  :
      nested.seatStatus !== undefined ? nested.seatStatus :
      seat.status      !== undefined ? seat.status      :
      seat.availStatus !== undefined ? seat.availStatus :
      nested.availStatus !== undefined ? nested.availStatus :
      undefined;

    // If no status field at all, cannot confirm unavailable — treat as available
    if (st === undefined) return true;
    return !UNAVAILABLE.has(st);
  }

  // Collect up to 5 sample seats for debug logging (emitted only on miss)
  const debugSamples = [];

  for (const group of renderData.renderGroups) {
    const seats = group.seats || group.seatData || group.Seats || [];
    for (const seat of (Array.isArray(seats) ? seats : [])) {
      const row       = resolveRow(seat);
      const available = resolveAvailable(seat);

      if (debugSamples.length < 5) {
        const nested = seat.seat || {};
        debugSamples.push({
          resolvedRow:    row,
          resolvedStatus: seat.seatStatus ?? nested.seatStatus ?? seat.status ?? seat.availStatus ?? nested.availStatus ?? '(none)',
          available,
          raw: seat,
        });
      }

      if (!row || !wantedRows.has(row)) continue;
      if (available) {
        return { hasData: true, available: true, matchedRow: row };
      }
    }
  }

  // Debug: only fires when preferred rows exist but nothing matched — helps
  // diagnose wrong field names without spamming normal runs.
  if (preferredRows && preferredRows.length > 0 && debugSamples.length > 0) {
    console.warn('[checkRowsInSeatLayout] Preferred rows not matched. Sample seats:');
    debugSamples.forEach((s, i) => {
      console.warn(
        `  seat[${i}] resolvedRow=${s.resolvedRow} status=${s.resolvedStatus} available=${s.available}`,
        s.raw
      );
    });
  }

  return { hasData: true, available: false, matchedRow: null };
}

// ────────────────────────────────────────────────────────────────────────────
// Check if HTML contains Cloudflare challenges
// ────────────────────────────────────────────────────────────────────────────
function isBlockedByCloudflare(html) {
  if (!html) return false;
  if (html.includes('cf-turnstile')) return true;
  if (html.includes('challenges.cloudflare.com')) return true;
  if (html.match(/<title[^>]*>Just a moment/i)) return true;
  return false;
}

// ────────────────────────────────────────────────────────────────────────────
// Mark a monitor terminal: update local storage, clear alarm, notify backend.
// Use for blocked_by_cloudflare and error — states that must never restart.
// ────────────────────────────────────────────────────────────────────────────
async function markMonitorTerminal(monitorId, status, message) {
  await updateMonitorLocally(monitorId, { status });
  chrome.alarms.clear(`bms-${monitorId}`);
  console.log(`[markMonitorTerminal] ${monitorId} → ${status}`);
  await notifyBackend(monitorId, { status, message });
}

// ────────────────────────────────────────────────────────────────────────────
// Poll listing mode: fetch listing page, find session, possibly upgrade to session mode
// ────────────────────────────────────────────────────────────────────────────
async function pollListingMonitor(monitor) {
  try {
    console.log(`[pollListingMonitor] Polling ${monitor.id}: ${monitor.listingUrl}`);

    const html = await fetchBMSPage(monitor.listingUrl);

    if (isBlockedByCloudflare(html)) {
      await markMonitorTerminal(monitor.id, 'blocked_by_cloudflare',
        '🛡️ Cloudflare challenge detected — BMS blocked the server. Monitoring stopped.');
      return;
    }

    const state = extractInitialState(html);
    if (!state) {
      console.warn(`[pollListingMonitor] Could not extract state for ${monitor.id}`);
      await notifyBackend(monitor.id, {
        status: 'waiting_for_session',
        message: '⏳ Listing page loaded but state parsing failed. Retrying...'
      });
      return;
    }

    // Navigate: state.venueShowtimesFunctionalApi.queries[key].data.showDetailsTransformed.Event[]
    if (!state.venueShowtimesFunctionalApi) {
      console.warn(`[pollListingMonitor] No venueShowtimesFunctionalApi in state`);
      await notifyBackend(monitor.id, {
        status: 'waiting_for_session',
        message: '⏳ Waiting for session to open...'
      });
      return;
    }

    const queries = state.venueShowtimesFunctionalApi.queries || {};
    const queryKey = `getShowtimesByVenue-${monitor.venueCode}-${monitor.date}`;
    const queryData = queries[queryKey];

    if (!queryData || !queryData.data) {
      console.warn(`[pollListingMonitor] No query data for key: ${queryKey}`);
      await notifyBackend(monitor.id, {
        status: 'waiting_for_session',
        message: '⏳ Waiting for session to open...'
      });
      return;
    }

    const showDetailsTransformed = queryData.data.showDetailsTransformed;
    if (!showDetailsTransformed || !showDetailsTransformed.Event) {
      console.warn(`[pollListingMonitor] No Event array in showDetailsTransformed`);
      await notifyBackend(monitor.id, {
        status: 'waiting_for_session',
        message: '⏳ Waiting for session to open...'
      });
      return;
    }

    let foundSession = null;

    // Walk Event[] -> ChildEvents[] -> ShowTimes[]
    // EventCode lives on Event, not ChildEvent — filter one level up
    for (const event of showDetailsTransformed.Event || []) {
      if (monitor.eventCode && event.EventCode !== monitor.eventCode) continue;
      if (!event.ChildEvents) continue;

      for (const childEvent of event.ChildEvents) {
        if (!childEvent.ShowTimes) continue;

        for (const show of childEvent.ShowTimes) {
          // BMS uses PascalCase: ShowTime, SessionId, AvailStatus
          const normalized = normalizeShowtime(show.ShowTime);
          if (normalized === normalizeShowtime(monitor.targetShowtime)) {
            if (show.AvailStatus === '1') {
              foundSession = show;
              break;
            }
          }
        }

        if (foundSession) break;
      }

      if (foundSession) break;
    }

    if (foundSession) {
      const sessionId  = String(foundSession.SessionId || foundSession.sessionId || '');
      const bookingUrl = buildSeatLayoutUrl(monitor, sessionId);
      console.log(`[pollListingMonitor] Session ${sessionId} found for ${monitor.id}`);

      if (!monitor.preferredRows || monitor.preferredRows.length === 0) {
        // No row preference — alert immediately on session open
        const alertResult = await triggerAlert(monitor.id);
        await handleAlertResult(monitor.id, alertResult, `Show is open at ${monitor.showtime || monitor.targetShowtime}.`);
      } else {
        // Row preference set — transition to session mode for seat-level check.
        // Persist sessionId + bookingUrl so the next poll uses pollSessionMonitor.
        await updateMonitorLocally(monitor.id, {
          status:     'checking_rows',
          mode:       'session',        // switch routing for next alarm
          sessionId:  sessionId,
          bookingUrl: bookingUrl
        });
        await notifyBackend(monitor.id, {
          status:      'session_resolved',
          message:     `🔄 Session resolved (${sessionId}). Switching to seat-level row check for rows ${monitor.preferredRows.join(', ')}…`,
          session_id:  sessionId,
          booking_url: bookingUrl
        });
        console.log(`[pollListingMonitor] Transitioned ${monitor.id} to session mode — rows ${monitor.preferredRows.join(',')}`);
      }
    } else {
      // No session yet
      console.log(`[pollListingMonitor] No matching session found yet for ${monitor.id}`);
      await notifyBackend(monitor.id, {
        status: 'waiting_for_session',
        message: '⏳ Waiting for session to open...'
      });
    }
  } catch (err) {
    console.error(`[pollListingMonitor] Error for ${monitor.id}:`, err);
    await markMonitorTerminal(monitor.id, 'error', `❌ Unrecoverable poll error: ${err.message}`);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Poll session mode: fetch seat layout, check for available seats in preferred rows
// ────────────────────────────────────────────────────────────────────────────
async function pollSessionMonitor(monitor) {
  try {
    console.log(`[pollSessionMonitor] Polling ${monitor.id}: ${monitor.bookingUrl}`);

    const html = await fetchBMSPage(monitor.bookingUrl);

    if (isBlockedByCloudflare(html)) {
      await markMonitorTerminal(monitor.id, 'blocked_by_cloudflare',
        '🛡️ Cloudflare challenge detected — BMS blocked the server. Monitoring stopped.');
      return;
    }

    const state = extractInitialState(html);
    if (!state) {
      console.warn(`[pollSessionMonitor] Could not extract state for ${monitor.id}`);
      await notifyBackend(monitor.id, {
        status: 'checking_rows',
        message: '⏳ Seat layout loaded but state parsing failed. Retrying...'
      });
      return;
    }

    // Navigate: state.seatlayoutMovies.seatLayoutData.currentVenue.ShowTimes
    if (!state.seatlayoutMovies || !state.seatlayoutMovies.seatLayoutData) {
      console.warn(`[pollSessionMonitor] No seatlayoutMovies.seatLayoutData in state`);
      await notifyBackend(monitor.id, {
        status: 'checking_rows',
        message: '⏳ Checking availability...'
      });
      return;
    }

    const currentVenue = state.seatlayoutMovies.seatLayoutData.currentVenue;
    if (!currentVenue || !currentVenue.ShowTimes) {
      console.warn(`[pollSessionMonitor] No currentVenue.ShowTimes in state`);
      await notifyBackend(monitor.id, {
        status: 'checking_rows',
        message: '⏳ Checking availability...'
      });
      return;
    }

    // Find the show matching our sessionId
    let targetShow = null;
    for (const show of currentVenue.ShowTimes) {
      if (show.sessionid === monitor.sessionId || show.SessionId === monitor.sessionId) {
        targetShow = show;
        break;
      }
    }

    if (!targetShow) {
      console.warn(`[pollSessionMonitor] Show with sessionId ${monitor.sessionId} not found`);
      await notifyBackend(monitor.id, {
        status: 'checking_rows',
        message: '⏳ Checking availability...'
      });
      return;
    }

    const categories = targetShow.Categories || [];

    if (!monitor.preferredRows || monitor.preferredRows.length === 0) {
      // No row preference: alert if any category is available
      const hasAvailable = categories.some(cat => cat.AvailStatus === '1');
      if (hasAvailable) {
        console.log(`[pollSessionMonitor] Any-seat availability confirmed for ${monitor.id}`);
        const alertResult = await triggerAlert(monitor.id);
        await handleAlertResult(monitor.id, alertResult, 'Seats are available for your session.');
      } else {
        await notifyBackend(monitor.id, {
          status:  'checking_rows',
          message: '⏳ All sections still sold out. Checking again next poll…'
        });
      }
    } else {
      // Row preference set — must confirm at seat level before alerting.
      // Fast exit: if no category is open, preferred rows can't be open either.
      const anyCategoryOpen = categories.some(cat => cat.AvailStatus === '1');

      if (!anyCategoryOpen) {
        await notifyBackend(monitor.id, {
          status:  'checking_rows',
          message: `⏳ All sections sold out. Watching rows ${monitor.preferredRows.join(', ')}…`
        });
        return;
      }

      // At least one category open — check actual seat rows in renderGroups.
      const rowCheck = checkRowsInSeatLayout(
        state.seatlayoutMovies.seatLayoutData,
        monitor.preferredRows
      );

      if (!rowCheck.hasData) {
        // renderGroups not present in state — cannot confirm rows. Do NOT alert.
        console.warn(`[pollSessionMonitor] Categories open but renderGroups absent for ${monitor.id}. Holding alert until seat data available.`);
        await notifyBackend(monitor.id, {
          status:  'checking_rows',
          message: `⏳ Sections open but seat-level data not yet loaded — still watching rows ${monitor.preferredRows.join(', ')}…`
        });
        return;
      }

      if (rowCheck.available) {
        console.log(`[pollSessionMonitor] Row ${rowCheck.matchedRow} confirmed available for ${monitor.id}`);
        const alertResult = await triggerAlert(monitor.id);
        await handleAlertResult(
          monitor.id,
          alertResult,
          `Row ${rowCheck.matchedRow} is open for your session.`
        );
      } else {
        await notifyBackend(monitor.id, {
          status:  'checking_rows',
          message: `⏳ Sections open but rows ${monitor.preferredRows.join(', ')} not yet available. Watching…`
        });
      }
    }
  } catch (err) {
    console.error(`[pollSessionMonitor] Error for ${monitor.id}:`, err);
    await markMonitorTerminal(monitor.id, 'error', `❌ Unrecoverable poll error: ${err.message}`);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Main polling dispatcher
// ────────────────────────────────────────────────────────────────────────────

// Terminal: never restart. Retryable: keep alarm running.
const TERMINAL_STATUSES  = new Set(['alert_sent', 'stopped', 'blocked_by_cloudflare', 'error']);
const RETRYABLE_STATUSES = new Set(['waiting_for_session', 'session_resolved', 'checking_rows', 'monitoring', 'alert_failed']);

async function pollMonitor(monitorId) {
  try {
    const monitors = await getMonitors();
    const monitor  = monitors.find(m => m.id === monitorId);

    if (!monitor) {
      console.warn(`[pollMonitor] Monitor ${monitorId} not found in storage — clearing alarm`);
      chrome.alarms.clear(`bms-${monitorId}`);
      return;
    }

    if (TERMINAL_STATUSES.has(monitor.status)) {
      console.log(`[pollMonitor] Monitor ${monitorId} is terminal (${monitor.status}) — clearing alarm`);
      chrome.alarms.clear(`bms-${monitorId}`);
      return;
    }

    if (!RETRYABLE_STATUSES.has(monitor.status)) {
      console.log(`[pollMonitor] Monitor ${monitorId} has unrecognised status (${monitor.status}) — skipping`);
      return;
    }

    // Route: listing mode polls until session found; once session_resolved or in
    // session mode poll the seat-layout page.
    const useSessionPoller =
      monitor.mode === 'session'          ||
      monitor.status === 'checking_rows'  ||
      monitor.status === 'monitoring'     ||
      monitor.status === 'alert_failed'   ||
      (monitor.mode === 'listing' && monitor.status === 'session_resolved');

    if (useSessionPoller) {
      await pollSessionMonitor(monitor);
    } else {
      await pollListingMonitor(monitor);
    }
  } catch (err) {
    console.error(`[pollMonitor] Unexpected error for ${monitorId}:`, err);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// POST update to backend
// ────────────────────────────────────────────────────────────────────────────
async function notifyBackend(monitorId, data) {
  try {
    const payload = {
      status: data.status,
      message: data.message,
      session_id: data.session_id || null,
      booking_url: data.booking_url || null
    };

    const resp = await fetch(`${SERVER}/api/monitor/${monitorId}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!resp.ok) {
      console.warn(`[notifyBackend] Server returned ${resp.status} for monitor ${monitorId}`);
    }
  } catch (err) {
    console.error(`[notifyBackend] Error posting update for ${monitorId}:`, err);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// POST alert trigger to backend — returns { ok, status, message }
// Callers MUST act on the return value:
//   ok=true  → mark terminal locally, clear alarm
//   ok=false → keep polling, log failure
// ────────────────────────────────────────────────────────────────────────────
async function triggerAlert(monitorId) {
  try {
    const resp = await fetch(`${SERVER}/api/monitor/${monitorId}/alert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });

    let data = {};
    try { data = await resp.json(); } catch { /* body not JSON */ }

    if (resp.ok && data.ok) {
      console.log(`[triggerAlert] ✅ Alert sent for ${monitorId}`);
      return { ok: true, status: data.status || 'alert_sent', message: data.message || 'Alert sent' };
    }

    const reason = data.message || `HTTP ${resp.status}`;
    console.warn(`[triggerAlert] ⚠️ Alert delivery failed for ${monitorId}: ${reason}`);
    return { ok: false, status: 'alert_failed', message: reason };

  } catch (err) {
    console.error(`[triggerAlert] ❌ Network error for ${monitorId}:`, err);
    return { ok: false, status: 'alert_failed', message: err.message };
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Shared: finalise alert result — called after triggerAlert() returns
// ────────────────────────────────────────────────────────────────────────────
async function handleAlertResult(monitorId, alertResult, successMessage) {
  if (alertResult.ok) {
    await updateMonitorLocally(monitorId, { status: 'alert_sent' });
    chrome.alarms.clear(`bms-${monitorId}`);
    console.log(`[handleAlertResult] Monitor ${monitorId} complete — alarm cleared`);
    await notifyBackend(monitorId, {
      status: 'alert_sent',
      message: `✅ ${successMessage} Monitoring stopped.`
    });
  } else {
    // Keep status retryable — next poll will attempt again
    await updateMonitorLocally(monitorId, { status: 'alert_failed' });
    await notifyBackend(monitorId, {
      status: 'alert_failed',
      message: `⚠️ Alert delivery failed (${alertResult.message}). Will retry on next poll.`
    });
    console.warn(`[handleAlertResult] Monitor ${monitorId} alert failed — will retry`);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Update monitor status locally in storage
// ────────────────────────────────────────────────────────────────────────────
async function updateMonitorLocally(monitorId, updates) {
  try {
    const monitors = await getMonitors();
    const idx = monitors.findIndex(m => m.id === monitorId);
    if (idx !== -1) {
      monitors[idx] = { ...monitors[idx], ...updates };
      await chrome.storage.local.set({ monitors });
      console.log(`[updateMonitorLocally] Updated monitor ${monitorId}:`, updates);
    }
  } catch (err) {
    console.error(`[updateMonitorLocally] Error:`, err);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Storage helpers
// ────────────────────────────────────────────────────────────────────────────
async function getMonitors() {
  try {
    const data = await chrome.storage.local.get('monitors');
    return data.monitors || [];
  } catch (err) {
    console.error('[getMonitors] Error:', err);
    return [];
  }
}

async function saveMonitors(monitors) {
  try {
    await chrome.storage.local.set({ monitors });
  } catch (err) {
    console.error('[saveMonitors] Error:', err);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Re-attach alarms for active monitors on service worker startup
// ────────────────────────────────────────────────────────────────────────────
async function resumeMonitors() {
  try {
    const monitors = await getMonitors();
    let resumed = 0;

    for (const monitor of monitors) {
      // Never re-attach alarms for terminal or unknown statuses
      if (!RETRYABLE_STATUSES.has(monitor.status)) {
        console.log(`[resumeMonitors] Skipping ${monitor.id} — terminal status: ${monitor.status}`);
        continue;
      }
      const interval = monitor.pollInterval || 30;
      console.log(`[resumeMonitors] Re-attaching alarm for ${monitor.id} (${monitor.status}, interval: ${interval}s)`);
      chrome.alarms.create(`bms-${monitor.id}`, { periodInMinutes: Math.max(1, interval / 60) });
      resumed++;
    }

    console.log(`[resumeMonitors] Resumed ${resumed} monitor(s)`);
  } catch (err) {
    console.error('[resumeMonitors] Error:', err);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Message listener for popup.js commands
// ────────────────────────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'startMonitor') {
    (async () => {
      try {
        const monitorId = msg.monitorId;
        const interval = msg.pollInterval || 30;
        const alarmName = `bms-${monitorId}`;

        console.log(`[onMessage] Starting monitor ${monitorId} with interval ${interval}s`);

        // Create alarm (minimum 1 minute per MV3)
        chrome.alarms.create(alarmName, {
          periodInMinutes: Math.max(1, interval / 60)
        });

        sendResponse({ ok: true });
      } catch (err) {
        console.error('[onMessage:startMonitor] Error:', err);
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (msg.action === 'stopMonitor') {
    try {
      const monitorId = msg.monitorId;
      const alarmName = `bms-${monitorId}`;

      console.log(`[onMessage] Stopping monitor ${monitorId}`);
      chrome.alarms.clear(alarmName);
      sendResponse({ ok: true });
    } catch (err) {
      console.error('[onMessage:stopMonitor] Error:', err);
      sendResponse({ ok: false, error: err.message });
    }
    return true;
  }

  return true;
});

// ────────────────────────────────────────────────────────────────────────────
// Alarm listener — fires when poll time arrives
// ────────────────────────────────────────────────────────────────────────────
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name.startsWith('bms-')) {
    const monitorId = alarm.name.substring(4);
    console.log(`[onAlarm] Poll triggered for ${monitorId}`);
    pollMonitor(monitorId);
  }
});

// ────────────────────────────────────────────────────────────────────────────
// Service worker startup: resume monitors from storage
// ────────────────────────────────────────────────────────────────────────────
console.log('[Service Worker] Initializing...');
resumeMonitors();
console.log('[Service Worker] Ready');
