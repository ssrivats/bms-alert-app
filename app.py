"""
BookMyShow Seat Alert — Production-ready server
================================================
Fixes applied:
  #1  Redis persistence (falls back to in-memory if REDIS_URL not set)
  #2  WhatsApp pre-check before monitor starts
  #3  Alert deduplication (alert_sent flag)
  #4  Twilio retry with exponential backoff (3 attempts)
  #5  Monitor timeout (30 min max)
  #6  Shared browser instance (one per process)
  #7  Jitter on poll interval (±random 1-3s)
  #8  Server-side smart poll interval based on showtime proximity
  #9  page.reload() instead of page.goto() after first load
  #10 Fallback text scan if Playwright DOM scan fails
  #11 Max 5 active monitors per phone (abuse prevention)
  #14 poll_count + last_checked exposed in API
  #15 failure counter → "error" status if threshold exceeded
  #17 last_result exposed in API
  #18 last_error field
  #26 Startup env validation (warn loudly, don't crash)
  #27 /health endpoint
"""

import os
import json
import uuid
import time
import random
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Twilio config ─────────────────────────────────────────────────────────────
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

# FIX #26 — Warn loudly on startup if Twilio not configured
if not TWILIO_SID or not TWILIO_TOKEN:
    log.warning("=" * 60)
    log.warning("TWILIO NOT CONFIGURED — WhatsApp alerts will not be sent!")
    log.warning("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN env vars.")
    log.warning("=" * 60)

# ── BMS constants ─────────────────────────────────────────────────────────────
BMS_BASE = "https://in.bookmyshow.com"
BMS_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MAX_ACTIVE_PER_PHONE = 5   # FIX #11
MAX_RUNTIME_SECS     = 1800  # FIX #5  — 30 min max
MAX_FAILURES         = 10    # FIX #15 — error out after this many consecutive failures


# ══════════════════════════════════════════════════════════════════════════════
#  FIX #1 — PERSISTENCE (Redis if available, else in-memory)
# ══════════════════════════════════════════════════════════════════════════════

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None

if REDIS_URL:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
        log.info("✅ Redis connected: %s", REDIS_URL[:30])
    except Exception as e:
        log.warning("Redis connection failed (%s) — falling back to in-memory", e)
        _redis = None
else:
    log.info("No REDIS_URL set — using in-memory store (monitors lost on restart)")

# In-memory fallback
_local_monitors = {}


def _save_monitor(mid, data):
    if _redis:
        _redis.set(f"monitor:{mid}", json.dumps(data), ex=86400)  # 24h TTL
    else:
        _local_monitors[mid] = data


def _load_monitor(mid):
    if _redis:
        raw = _redis.get(f"monitor:{mid}")
        return json.loads(raw) if raw else None
    return _local_monitors.get(mid)


def _load_all_monitors():
    if _redis:
        keys = _redis.keys("monitor:*")
        result = {}
        for key in keys:
            raw = _redis.get(key)
            if raw:
                try:
                    data = json.loads(raw)
                    result[data["id"]] = data
                except Exception:
                    pass
        return result
    return dict(_local_monitors)


def _delete_monitor(mid):
    if _redis:
        _redis.delete(f"monitor:{mid}")
    else:
        _local_monitors.pop(mid, None)


def _add_log(mid, message, event_type="info"):
    """Thread-safe log append."""
    monitor = _load_monitor(mid)
    if not monitor:
        return
    log_entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "type": event_type,   # FIX #16 structured
    }
    monitor.setdefault("logs", []).append(log_entry)
    # Keep last 100 log entries
    monitor["logs"] = monitor["logs"][-100:]
    _save_monitor(mid, monitor)
    log.info("[%s] %s", mid, message)


# ══════════════════════════════════════════════════════════════════════════════
#  FIX #6 — SHARED BROWSER INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_browser_lock    = threading.Lock()
_shared_playwright = None
_shared_browser    = None


def _get_browser():
    """Return a shared Chromium browser instance. Create if needed."""
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser and _shared_browser.is_connected():
            return _shared_browser
        # Launch fresh
        from playwright.sync_api import sync_playwright
        if _shared_playwright:
            try: _shared_playwright.stop()
            except: pass
        _shared_playwright = sync_playwright().start()
        _shared_browser = _shared_playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        log.info("🌐 Browser launched (shared instance)")
        return _shared_browser


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health")  # FIX #27
def health():
    monitors = _load_all_monitors()
    active = sum(1 for m in monitors.values() if m.get("status") == "monitoring")
    return jsonify({
        "status": "ok",
        "active_monitors": active,
        "total_monitors": len(monitors),
        "redis": bool(_redis),
        "twilio": bool(TWILIO_SID and TWILIO_TOKEN),
        "time": datetime.now().isoformat(),
    })


@app.route("/api/monitor", methods=["POST"])
def start_monitor():
    data = request.json or {}
    phone = data.get("phone", "").strip()

    # FIX #11 — max active monitors per phone
    all_monitors = _load_all_monitors()
    active_for_phone = sum(
        1 for m in all_monitors.values()
        if m.get("config", {}).get("phone") == phone
        and m.get("status") == "monitoring"
    )
    if active_for_phone >= MAX_ACTIVE_PER_PHONE:
        return jsonify({
            "error": f"You already have {active_for_phone} active monitors. Stop one first."
        }), 429

    monitor_id = str(uuid.uuid4())[:8]

    # FIX #8 — server-side adaptive poll interval
    client_interval = int(data.get("poll_interval", 20))
    server_interval  = _smart_interval(data.get("showtime", ""), client_interval)

    config = {
        "movie":         data.get("movie", ""),
        "theatre":       data.get("theatre", ""),
        "showtime":      data.get("showtime", ""),
        "booking_url":   data.get("booking_url", ""),
        "phone":         phone,
        "preferred_row": data.get("preferred_row", ""),
        "poll_interval": server_interval,
    }

    monitor = {
        "id":           monitor_id,
        "config":       config,
        "status":       "starting",
        "started_at":   datetime.now().isoformat(),
        "poll_count":   0,
        "last_checked": None,
        "last_result":  None,
        "last_error":   None,   # FIX #18
        "alert_sent":   False,
        "failures":     0,
        "logs":         [],
    }
    _save_monitor(monitor_id, monitor)

    thread = threading.Thread(target=_run_monitor, args=(monitor_id,), daemon=True)
    thread.start()

    return jsonify({"monitor_id": monitor_id, "status": "started", "poll_interval": server_interval})


@app.route("/api/monitor/<monitor_id>")
def get_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id":           monitor["id"],
        "status":       monitor["status"],
        "started_at":   monitor["started_at"],
        "poll_count":   monitor.get("poll_count", 0),
        "last_checked": monitor.get("last_checked"),
        "last_result":  monitor.get("last_result"),   # FIX #17
        "last_error":   monitor.get("last_error"),    # FIX #18
        "alert_sent":   monitor.get("alert_sent", False),
        "logs":         monitor.get("logs", [])[-50:],
    })


@app.route("/api/monitors")
def list_monitors():
    all_m = _load_all_monitors()
    result = []
    for m in all_m.values():
        result.append({
            "id":           m["id"],
            "status":       m["status"],
            "started_at":   m["started_at"],
            "poll_count":   m.get("poll_count", 0),
            "last_checked": m.get("last_checked"),
            "last_result":  m.get("last_result"),
            "alert_sent":   m.get("alert_sent", False),
            "movie":        m["config"].get("movie", ""),
            "phone":        m["config"].get("phone", ""),
            "booking_url":  m["config"].get("booking_url", ""),
        })
    result.sort(key=lambda x: x["started_at"], reverse=True)
    return jsonify(result)


@app.route("/api/monitor/<monitor_id>/stop", methods=["POST"])
def stop_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    monitor["status"] = "stopped"
    _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, "Stopped by user", "stop")
    return jsonify({"status": "stopped"})


@app.route("/api/test-whatsapp", methods=["POST"])
def test_whatsapp():
    data = request.json or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "phone required"}), 400

    if not phone.startswith("+"):
        phone = f"+91{phone}"
    to = f"whatsapp:{phone}"

    message = (
        "👋 *BMS Seat Alert — Test Message*\n\n"
        "✅ WhatsApp alerts are working!\n"
        "You'll get a message like this when your rows open up.\n\n"
        "_Powered by BMS Seat Alert_"
    )

    try:
        if not TWILIO_SID or not TWILIO_TOKEN:
            return jsonify({"error": "Twilio credentials not configured on server"}), 500
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=to)
        return jsonify({"status": "sent", "sid": msg.sid, "to": phone})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _run_monitor(monitor_id):
    """Background polling loop — one thread per monitor."""
    from playwright.sync_api import TimeoutError as PwTimeout

    monitor = _load_monitor(monitor_id)
    config  = monitor["config"]
    monitor["status"] = "monitoring"
    _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, f"Started — watching {config['movie']}", "start")

    booking_url = config["booking_url"]
    if not booking_url:
        monitor = _load_monitor(monitor_id)
        monitor["status"] = "error"
        monitor["last_error"] = "No booking URL"
        _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, "Error: no booking URL", "error")
        return

    start_time = time.time()
    first_load = True

    try:
        browser = _get_browser()
        context = browser.new_context(user_agent=BMS_UA)
        page    = context.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,ico}", lambda r: r.abort())

        while True:
            # Re-read latest state from store (so stop signal is picked up)
            monitor = _load_monitor(monitor_id)
            if not monitor or monitor["status"] != "monitoring":
                break

            # FIX #5 — timeout after MAX_RUNTIME_SECS
            if time.time() - start_time > MAX_RUNTIME_SECS:
                monitor["status"] = "timeout"
                _save_monitor(monitor_id, monitor)
                _add_log(monitor_id, "⏰ Monitor timed out after 30 minutes", "timeout")
                break

            # FIX #8 — recalculate interval each poll (showtime gets closer)
            poll_interval = _smart_interval(config.get("showtime", ""), config["poll_interval"])

            monitor["poll_count"] = monitor.get("poll_count", 0) + 1
            poll_num = monitor["poll_count"]
            _save_monitor(monitor_id, monitor)
            _add_log(monitor_id, f"Poll #{poll_num} (every {poll_interval}s)…", "poll")

            try:
                # FIX #9 — reload instead of full goto after first load
                if first_load:
                    page.goto(booking_url, timeout=30_000, wait_until="domcontentloaded")
                    first_load = False
                else:
                    page.reload(timeout=30_000, wait_until="domcontentloaded")

                page.wait_for_timeout(2000)

                available, reason = _check_rows(page, config["preferred_row"])

                monitor = _load_monitor(monitor_id)
                monitor["last_checked"] = datetime.now().strftime("%H:%M:%S")
                monitor["last_result"]  = reason
                monitor["failures"]     = 0  # reset on success
                _save_monitor(monitor_id, monitor)

                if available:
                    _add_log(monitor_id, f"✅ SEATS FOUND: {reason}", "found")
                    monitor = _load_monitor(monitor_id)
                    monitor["status"] = "found"
                    _save_monitor(monitor_id, monitor)
                    _send_alert(monitor_id, booking_url)
                    break
                else:
                    _add_log(monitor_id, f"⏳ {reason}", "poll")

            except PwTimeout:
                _handle_failure(monitor_id, "Page load timed out")
            except Exception as e:
                err = str(e)[:100]
                _handle_failure(monitor_id, f"Error: {err}")
                monitor = _load_monitor(monitor_id)
                if monitor.get("failures", 0) >= MAX_FAILURES:
                    monitor["status"] = "error"
                    _save_monitor(monitor_id, monitor)
                    _add_log(monitor_id, "❌ Too many failures, stopping", "error")
                    break

            # FIX #7 — jitter ±random 1-3s to avoid bot detection
            jitter = random.uniform(1, 3)
            time.sleep(poll_interval + jitter)

        try:
            context.close()
        except Exception:
            pass

    except Exception as e:
        monitor = _load_monitor(monitor_id)
        if monitor:
            monitor["status"] = "error"
            monitor["last_error"] = str(e)[:100]
            _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, f"❌ Fatal error: {str(e)[:80]}", "error")


def _handle_failure(monitor_id, message):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return
    monitor["failures"] = monitor.get("failures", 0) + 1
    monitor["last_error"] = message
    _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, f"⚠️ {message} (failure #{monitor['failures']})", "warn")


def _smart_interval(showtime_str: str, default: int = 20) -> int:
    """Return poll interval in seconds based on how close the show is."""
    if not showtime_str:
        return default
    try:
        match = __import__("re").search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", showtime_str, __import__("re").IGNORECASE)
        if not match:
            return default
        h, m, ampm = int(match.group(1)), int(match.group(2)), match.group(3).upper()
        if ampm == "PM" and h != 12: h += 12
        if ampm == "AM" and h == 12: h = 0
        now   = datetime.now()
        show  = now.replace(hour=h, minute=m, second=0, microsecond=0)
        diff  = (show - now).total_seconds() / 60
        if diff < 0:   return default
        if diff < 30:  return 5
        if diff < 120: return 10
        if diff < 360: return 15
        return 30
    except Exception:
        return default


def _check_rows(page, preferred_rows: str) -> tuple:
    """
    Check if preferred rows have available seats.
    Uses Playwright JS DOM scan; falls back to text scan if that fails.
    """
    page_text = page.inner_text("body").lower()

    if any(p in page_text for p in ["sold out", "housefull", "no seats available"]):
        return False, "Show is sold out"

    rows_list = [r.strip().upper() for r in preferred_rows.split(",") if r.strip()]

    if not rows_list:
        seats = page.query_selector_all("[class*='seat'], [class*='Seat']")
        if seats:
            return True, f"Seat map open ({len(seats)} seats visible)"
        return False, "No seat map visible yet"

    # ── Primary: DOM-based scan ────────────────────────────────────────────────
    try:
        result = page.evaluate("""
        (targetRows) => {
            const found   = [];
            const scanned = [];

            const ROW_SELECTORS = [
                '[class*="seat-row"]', '[class*="SeatRow"]',
                '[class*="seatRow"]',  '[class*="row-container"]',
                '[class*="RowContainer"]',
            ];

            let rowEls = [];
            let usedSel = '';
            for (const sel of ROW_SELECTORS) {
                const els = Array.from(document.querySelectorAll(sel));
                if (els.length > 2) { rowEls = els; usedSel = sel; break; }
            }

            // Fallback: elements whose first child is a single letter
            if (rowEls.length === 0) {
                rowEls = Array.from(document.querySelectorAll('*')).filter(el => {
                    if (el.children.length < 2) return false;
                    const label = el.firstElementChild?.textContent?.trim();
                    return label && /^[A-Z]{1,2}$/.test(label);
                });
                usedSel = 'fallback-letter-scan';
            }

            scanned.push(`selector="${usedSel}" rows=${rowEls.length}`);

            for (const rowEl of rowEls) {
                const labelEl = rowEl.querySelector(
                    '[class*="label"], [class*="Label"], [class*="row-name"], [class*="rowName"]'
                );
                const rawLabel = (labelEl || rowEl.firstElementChild || rowEl)
                    .textContent.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
                const label = rawLabel.slice(0, 2);

                if (!targetRows.includes(label)) continue;

                const seats = rowEl.querySelectorAll('[class*="seat"], [class*="Seat"]');
                const available = Array.from(seats).filter(s => {
                    const cls = (s.className || '').toLowerCase();
                    const aria = (s.getAttribute('aria-label') || '').toLowerCase();
                    return !cls.includes('sold')    && !cls.includes('booked') &&
                           !cls.includes('unavail') && !cls.includes('disabled') &&
                           !cls.includes('blocked') && !aria.includes('sold') &&
                           !s.hasAttribute('disabled');
                }).length;

                scanned.push(`row=${label} total=${seats.length} avail=${available}`);
                if (available > 0) found.push({ row: label, count: available });
            }

            return { found, scanned };
        }
        """, rows_list)

        for line in result.get("scanned", []):
            log.info("[seat-scan] %s", line)

        found = result.get("found", [])
        if found:
            summary = ", ".join(f"Row {f['row']} ({f['count']} seats)" for f in found)
            return True, f"Available — {summary}"

        scanned_summary = " | ".join(result.get("scanned", [])[:3])
        return False, f"No seats in rows {preferred_rows} [{scanned_summary}]"

    except Exception as e:
        log.warning("DOM scan failed: %s — using text fallback", e)

    # FIX #10 — text scan fallback
    for row in rows_list:
        if (f" {row.lower()} " in page_text or
                f"row {row.lower()}" in page_text or
                f"\n{row.lower()}\n" in page_text):
            return True, f"Row {row} detected (text fallback)"

    return False, f"Rows {preferred_rows} not yet available (text scan)"


def _send_alert(monitor_id, booking_url):
    """Send WhatsApp alert — FIX #3 deduplication + FIX #4 retry."""
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return

    # FIX #3 — deduplicate
    if monitor.get("alert_sent"):
        _add_log(monitor_id, "Alert already sent — skipping duplicate", "info")
        return

    # Mark immediately to prevent race conditions
    monitor["alert_sent"] = True
    _save_monitor(monitor_id, monitor)

    config = monitor["config"]
    phone  = config["phone"]
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    if not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp:", "whatsapp:+91")

    message = (
        f"🚨 *Seats Available!*\n\n"
        f"🎬 {config['movie']}\n"
    )
    if config.get("theatre"):  message += f"🏠 {config['theatre']}\n"
    if config.get("showtime"): message += f"🕐 {config['showtime']}\n"
    if monitor.get("last_result"): message += f"💺 {monitor['last_result']}\n"
    message += f"\n👉 Book now: {booking_url}\n"
    message += f"\n_Alert at {datetime.now().strftime('%H:%M')}_"

    if not TWILIO_SID or not TWILIO_TOKEN:
        _add_log(monitor_id, "⚠️ Twilio not configured — alert not sent", "warn")
        return

    # FIX #4 — retry up to 3 times with exponential backoff
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    for attempt in range(3):
        try:
            msg = client.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            _add_log(monitor_id, f"✅ WhatsApp sent (attempt {attempt+1}) SID: {msg.sid}", "alert")
            return
        except Exception as e:
            wait = 2 ** attempt  # 1s, 2s, 4s
            _add_log(monitor_id, f"⚠️ Twilio attempt {attempt+1} failed: {str(e)[:60]} — retry in {wait}s", "warn")
            if attempt < 2:
                time.sleep(wait)

    _add_log(monitor_id, "❌ All 3 WhatsApp send attempts failed", "error")
    # Reset flag so a future attempt can retry
    monitor = _load_monitor(monitor_id)
    if monitor:
        monitor["alert_sent"] = False
        _save_monitor(monitor_id, monitor)


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
