"""
BookMyShow Seat Alert backend.

Honest v1 behavior:
- monitor one BookMyShow session at a time
- optionally filter by selected section / price tier
- use BookMyShow's embedded __INITIAL_STATE__ instead of scraping canvas DOM
"""

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

if not TWILIO_SID or not TWILIO_TOKEN:
    log.warning("TWILIO NOT CONFIGURED — WhatsApp alerts will not be sent")

BMS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MAX_ACTIVE_PER_PHONE = 5
MAX_RUNTIME_SECS = 1800
MAX_FAILURES = 10

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis = None
_local_monitors = {}

if REDIS_URL:
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
    except Exception as exc:
        log.warning("Redis connection failed (%s) — using in-memory store", exc)
        _redis = None


def _save_monitor(monitor_id, data):
    if _redis:
      _redis.set(f"monitor:{monitor_id}", json.dumps(data), ex=86400)
    else:
      _local_monitors[monitor_id] = data


def _load_monitor(monitor_id):
    if _redis:
        raw = _redis.get(f"monitor:{monitor_id}")
        return json.loads(raw) if raw else None
    return _local_monitors.get(monitor_id)


def _load_all_monitors():
    if _redis:
        result = {}
        for key in _redis.keys("monitor:*"):
            raw = _redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                result[data["id"]] = data
            except Exception:
                continue
        return result
    return dict(_local_monitors)


def _add_log(monitor_id, message, event_type="info"):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "type": event_type,
    }
    monitor.setdefault("logs", []).append(entry)
    monitor["logs"] = monitor["logs"][-100:]
    _save_monitor(monitor_id, monitor)
    log.info("[%s] %s", monitor_id, message)


_browser_lock = threading.Lock()
_shared_playwright = None
_shared_browser = None


def _get_browser():
    global _shared_playwright, _shared_browser
    with _browser_lock:
        if _shared_browser and _shared_browser.is_connected():
            return _shared_browser

        from playwright.sync_api import sync_playwright

        if _shared_playwright:
            try:
                _shared_playwright.stop()
            except Exception:
                pass

        _shared_playwright = sync_playwright().start()
        _shared_browser = _shared_playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        return _shared_browser


@app.route("/health")
def health():
    monitors = _load_all_monitors()
    active = sum(1 for monitor in monitors.values() if monitor.get("status") == "monitoring")
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

    active_for_phone = sum(
        1 for monitor in _load_all_monitors().values()
        if monitor.get("config", {}).get("phone") == phone
        and monitor.get("status") == "monitoring"
    )
    if active_for_phone >= MAX_ACTIVE_PER_PHONE:
        return jsonify({"error": f"You already have {active_for_phone} active monitors. Stop one first."}), 429

    booking_url = data.get("booking_url", "")
    url_match = re.search(r"/seat-layout/[^/]+/[^/]+/([^/]+)/", booking_url)
    session_id = url_match.group(1) if url_match else str(data.get("show_id", "")).strip()
    preferred_categories = [
        str(code).strip().upper()
        for code in data.get("preferred_categories", []) or []
        if str(code).strip()
    ]

    monitor_id = str(uuid.uuid4())[:8]
    config = {
        "movie": data.get("movie", ""),
        "theatre": data.get("theatre", ""),
        "showtime": data.get("showtime", ""),
        "booking_url": booking_url,
        "phone": phone,
        "poll_interval": _smart_interval(data.get("showtime", ""), int(data.get("poll_interval", 20))),
        "session_id": session_id,
        "preferred_categories": preferred_categories,
    }

    monitor = {
        "id": monitor_id,
        "config": config,
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        "poll_count": 0,
        "last_checked": None,
        "last_result": None,
        "last_error": None,
        "alert_sent": False,
        "failures": 0,
        "logs": [],
    }
    _save_monitor(monitor_id, monitor)

    thread = threading.Thread(target=_run_monitor, args=(monitor_id,), daemon=True)
    thread.start()
    return jsonify({"monitor_id": monitor_id, "status": "started", "poll_interval": config["poll_interval"]})


@app.route("/api/monitor/<monitor_id>")
def get_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": monitor["id"],
        "status": monitor["status"],
        "started_at": monitor["started_at"],
        "poll_count": monitor.get("poll_count", 0),
        "last_checked": monitor.get("last_checked"),
        "last_result": monitor.get("last_result"),
        "last_error": monitor.get("last_error"),
        "alert_sent": monitor.get("alert_sent", False),
        "logs": monitor.get("logs", [])[-50:],
    })


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

    if not TWILIO_SID or not TWILIO_TOKEN:
        return jsonify({"error": "Twilio credentials not configured on server"}), 500

    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(
        body=(
            "👋 *BMS Seat Alert — Test Message*\n\n"
            "✅ WhatsApp alerts are working!\n"
            "You'll get a message like this when your selected section opens up.\n\n"
            "_Powered by BMS Seat Alert_"
        ),
        from_=TWILIO_FROM,
        to=f"whatsapp:{phone}",
    )
    return jsonify({"status": "sent", "sid": msg.sid, "to": phone})


def _run_monitor(monitor_id):
    from playwright.sync_api import TimeoutError as PwTimeout

    monitor = _load_monitor(monitor_id)
    config = monitor["config"]
    monitor["status"] = "monitoring"
    _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, f"Started — watching {config['movie']}", "start")

    if not config["booking_url"]:
        monitor["status"] = "error"
        monitor["last_error"] = "No booking URL"
        _save_monitor(monitor_id, monitor)
        return

    start_time = time.time()
    first_load = True

    try:
        browser = _get_browser()
        context = browser.new_context(
            user_agent=BMS_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,ico}", lambda route: route.abort())

        while True:
            monitor = _load_monitor(monitor_id)
            if not monitor or monitor["status"] != "monitoring":
                break

            if time.time() - start_time > MAX_RUNTIME_SECS:
                monitor["status"] = "timeout"
                _save_monitor(monitor_id, monitor)
                _add_log(monitor_id, "⏰ Monitor timed out after 30 minutes", "timeout")
                break

            poll_interval = _smart_interval(config.get("showtime", ""), config["poll_interval"])
            monitor["poll_count"] = monitor.get("poll_count", 0) + 1
            _save_monitor(monitor_id, monitor)
            _add_log(monitor_id, f"Poll #{monitor['poll_count']} (every {poll_interval}s)…", "poll")

            try:
                if first_load:
                    page.goto(config["booking_url"], timeout=30_000, wait_until="domcontentloaded")
                    first_load = False
                else:
                    page.reload(timeout=30_000, wait_until="domcontentloaded")

                page.wait_for_timeout(3000)
                available, reason = _check_availability(
                    page,
                    config.get("session_id", ""),
                    config.get("preferred_categories", []),
                )

                monitor = _load_monitor(monitor_id)
                monitor["last_checked"] = datetime.now().strftime("%H:%M:%S")
                monitor["last_result"] = reason
                monitor["failures"] = 0
                _save_monitor(monitor_id, monitor)

                if available:
                    monitor["status"] = "seats_found"
                    _save_monitor(monitor_id, monitor)
                    _add_log(monitor_id, f"✅ SEATS FOUND: {reason}", "found")
                    _send_alert(monitor_id, config["booking_url"])
                    break

                _add_log(monitor_id, f"⏳ {reason}", "poll")
            except PwTimeout:
                _handle_failure(monitor_id, "Page load timed out")
            except Exception as exc:
                _handle_failure(monitor_id, f"Error: {str(exc)[:100]}")
                monitor = _load_monitor(monitor_id)
                if monitor and monitor.get("failures", 0) >= MAX_FAILURES:
                    monitor["status"] = "error"
                    _save_monitor(monitor_id, monitor)
                    _add_log(monitor_id, "❌ Too many failures, stopping", "error")
                    break

            time.sleep(poll_interval + random.uniform(1, 3))

        try:
            context.close()
        except Exception:
            pass
    except Exception as exc:
        monitor = _load_monitor(monitor_id)
        if monitor:
            monitor["status"] = "error"
            monitor["last_error"] = str(exc)[:100]
            _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, f"❌ Fatal error: {str(exc)[:80]}", "error")


def _handle_failure(monitor_id, message):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return
    monitor["failures"] = monitor.get("failures", 0) + 1
    monitor["last_error"] = message
    _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, f"⚠️ {message} (failure #{monitor['failures']})", "warn")


def _smart_interval(showtime_str, default=20):
    if not showtime_str:
        return default
    try:
        match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", showtime_str, re.IGNORECASE)
        if not match:
            return default
        hours = int(match.group(1))
        mins = int(match.group(2))
        ampm = match.group(3).upper()
        if ampm == "PM" and hours != 12:
            hours += 12
        if ampm == "AM" and hours == 12:
            hours = 0
        now = datetime.now()
        show = now.replace(hour=hours, minute=mins, second=0, microsecond=0)
        diff = (show - now).total_seconds() / 60
        if diff < 0:
            return default
        if diff < 30:
            return 5
        if diff < 120:
            return 10
        if diff < 360:
            return 15
        return 30
    except Exception:
        return default


def _check_availability(page, session_id, preferred_categories):
    preferred_categories = [code.upper() for code in preferred_categories or []]

    try:
        state_data = page.evaluate("""
        (sessionId) => {
            try {
                const seatLayout = window.__INITIAL_STATE__?.seatlayoutMovies?.seatLayoutData;
                if (!seatLayout) return { error: "no_initial_state" };

                const venue = seatLayout.currentVenue;
                if (!venue) return { error: "no_venue" };

                const allShows = venue.ShowTimes || [];
                const show = allShows.find((item) => String(item.SessionId) === String(sessionId));
                if (!show) {
                    return {
                        error: "no_show_found",
                        allSessions: allShows.map((item) => ({ id: item.SessionId, time: item.ShowTime }))
                    };
                }

                return {
                    sessionId: show.SessionId,
                    showTime: show.ShowTime,
                    categories: (show.Categories || []).map((item) => ({
                        code: String(item.PriceCode || ""),
                        label: String(item.PriceDesc || item.PriceCode || ""),
                        availStatus: String(item.AvailStatus || ""),
                        range: String(item.CategoryRange || ""),
                        price: String(item.CurPrice || ""),
                    })),
                };
            } catch (error) {
                return { error: "js_exception: " + error.message };
            }
        }
        """, session_id)
    except Exception as exc:
        return False, f"State evaluation failed: {str(exc)[:80]}"

    if state_data.get("error"):
        return False, f"Could not read session data ({state_data['error']})"

    categories = state_data.get("categories", [])
    matching = [
        category for category in categories
        if category.get("availStatus") == "1" and category.get("range")
    ]

    if preferred_categories:
        matching = [category for category in matching if category.get("code", "").upper() in preferred_categories]
        if matching:
            summary = ", ".join(f"{cat['label']} ₹{cat['price']}" for cat in matching)
            return True, f"Selected sections open: {summary} ({state_data.get('showTime', '?')})"

        watched_labels = [
            category.get("label") or category.get("code")
            for category in categories
            if category.get("code", "").upper() in preferred_categories
        ]
        watched_text = ", ".join(watched_labels or preferred_categories)
        return False, f"Selected sections still sold out: {watched_text} ({state_data.get('showTime', '?')})"

    if matching:
        summary = ", ".join(f"{cat['label']} ₹{cat['price']}" for cat in matching)
        return True, f"Some section is open: {summary} ({state_data.get('showTime', '?')})"

    return False, f"All sections sold out ({state_data.get('showTime', '?')})"


def _send_alert(monitor_id, booking_url):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return

    if monitor.get("alert_sent"):
        _add_log(monitor_id, "Alert already sent — skipping duplicate", "info")
        return

    monitor["alert_sent"] = True
    _save_monitor(monitor_id, monitor)

    phone = monitor["config"]["phone"]
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    if not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp:", "whatsapp:+91")

    message = (
        f"🚨 *Seats Available!*\n\n"
        f"🎬 {monitor['config']['movie']}\n"
    )
    if monitor["config"].get("theatre"):
        message += f"🏠 {monitor['config']['theatre']}\n"
    if monitor["config"].get("showtime"):
        message += f"🕐 {monitor['config']['showtime']}\n"
    if monitor.get("last_result"):
        message += f"💺 {monitor['last_result']}\n"
    message += f"\n👉 Book now: {booking_url}\n"
    message += f"\n_Alert at {datetime.now().strftime('%H:%M')}_"

    if not TWILIO_SID or not TWILIO_TOKEN:
        _add_log(monitor_id, "⚠️ Twilio not configured — alert not sent", "warn")
        return

    client = Client(TWILIO_SID, TWILIO_TOKEN)
    for attempt in range(3):
        try:
            msg = client.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            _add_log(monitor_id, f"✅ WhatsApp sent (attempt {attempt + 1}) SID: {msg.sid}", "alert")
            return
        except Exception as exc:
            wait = 2 ** attempt
            _add_log(monitor_id, f"⚠️ Twilio attempt {attempt + 1} failed: {str(exc)[:60]} — retry in {wait}s", "warn")
            if attempt < 2:
                time.sleep(wait)

    monitor = _load_monitor(monitor_id)
    if monitor:
        monitor["alert_sent"] = False
        _save_monitor(monitor_id, monitor)
    _add_log(monitor_id, "❌ All 3 WhatsApp send attempts failed", "error")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
