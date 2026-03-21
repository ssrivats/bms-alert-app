"""
BookMyShow Seat Alert — Simple v1
==================================
Flask backend that monitors BMS seats and sends WhatsApp alerts.
"""

import os
import uuid
import time
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

# ── In-memory monitors ────────────────────────────────────────────────────────
monitors = {}  # monitor_id -> { config, status, logs[] }

# ── BMS headers ───────────────────────────────────────────────────────────────
BMS_BASE = "https://in.bookmyshow.com"
BMS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/monitor", methods=["POST"])
def start_monitor():
    """Start monitoring a show."""
    data = request.json
    monitor_id = str(uuid.uuid4())[:8]

    config = {
        "movie": data.get("movie", ""),
        "theatre": data.get("theatre", ""),
        "showtime": data.get("showtime", ""),
        "booking_url": data.get("booking_url", ""),
        "phone": data.get("phone", ""),
        "preferred_row": data.get("preferred_row", ""),  # e.g., "A,B,C"
        "poll_interval": int(data.get("poll_interval", 15)),
    }

    monitors[monitor_id] = {
        "id": monitor_id,
        "config": config,
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        "logs": [],
    }

    # Start monitoring in background
    thread = threading.Thread(
        target=_run_monitor,
        args=(monitor_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"monitor_id": monitor_id, "status": "started"})


@app.route("/api/monitor/<monitor_id>")
def get_monitor(monitor_id):
    """Get current status of a monitor."""
    monitor = monitors.get(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": monitor["id"],
        "status": monitor["status"],
        "logs": monitor["logs"][-10:],
    })


@app.route("/api/monitor/<monitor_id>/stop", methods=["POST"])
def stop_monitor(monitor_id):
    """Stop a monitor."""
    monitor = monitors.get(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404

    monitor["status"] = "stopped"
    _add_log(monitor_id, "Monitor stopped by user")
    return jsonify({"status": "stopped"})


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _add_log(monitor_id, message):
    """Add log entry."""
    monitors[monitor_id]["logs"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
    })
    log.info("[%s] %s", monitor_id, message)


def _run_monitor(monitor_id):
    """Background monitor loop."""
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    monitor = monitors[monitor_id]
    config = monitor["config"]
    monitor["status"] = "monitoring"
    _add_log(monitor_id, f"Monitoring {config['movie']} at {config['theatre']}")

    booking_url = config["booking_url"]
    if not booking_url:
        _add_log(monitor_id, "No booking URL provided")
        monitor["status"] = "error"
        return

    poll_count = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=BMS_HEADERS["User-Agent"])
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2}", lambda r: r.abort())

        while monitor["status"] == "monitoring":
            poll_count += 1
            _add_log(monitor_id, f"Poll #{poll_count}...")

            try:
                page.goto(booking_url, timeout=30_000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)

                available, msg = _check_rows(page, config["preferred_row"])

                if available:
                    _add_log(monitor_id, f"✅ AVAILABLE: {msg}")
                    monitor["status"] = "found"
                    _send_alert(monitor_id, booking_url)
                    break
                else:
                    _add_log(monitor_id, f"⏳ {msg}")

            except PwTimeout:
                _add_log(monitor_id, "⚠️ Timeout, retrying...")
            except Exception as e:
                _add_log(monitor_id, f"⚠️ Error: {str(e)[:80]}")

            if monitor["status"] == "monitoring":
                time.sleep(config["poll_interval"])

        browser.close()

    if monitor["status"] != "found":
        _add_log(monitor_id, "Monitor finished")


def _check_rows(page, preferred_rows: str) -> tuple:
    """Check if preferred rows are available."""
    page_text = page.inner_text("body").lower()

    # Check sold out
    if any(p in page_text for p in ["sold out", "housefull", "no seats"]):
        return False, "Show is sold out"

    # Check booking open
    if not preferred_rows:
        # Generic booking check
        try:
            if page.query_selector("a[href*='buytickets'], button.book"):
                return True, "Booking is open"
        except:
            pass
        return False, "Booking not yet open"

    # Check specific rows
    rows_list = [r.strip() for r in preferred_rows.split(",") if r.strip()]
    for row in rows_list:
        if f"row {row.lower()}" in page_text:
            return True, f"Row {row} available"

    return False, f"Rows {preferred_rows} not yet available"


def _send_alert(monitor_id, booking_url):
    """Send WhatsApp alert."""
    monitor = monitors[monitor_id]
    config = monitor["config"]
    phone = config["phone"]

    # Format phone
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    if not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp:", "whatsapp:+91")

    message = (
        f"🚨 *Seats Available!*\n\n"
        f"🎬 {config['movie']}\n"
        f"🏠 {config['theatre']}\n"
        f"🕐 {config['showtime']}\n\n"
        f"👉 Book now: {booking_url}\n\n"
        f"_Alert at {datetime.now().strftime('%H:%M %p')}_"
    )

    try:
        if TWILIO_SID and TWILIO_TOKEN:
            client = Client(TWILIO_SID, TWILIO_TOKEN)
            msg = client.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            _add_log(monitor_id, f"✅ WhatsApp sent! SID: {msg.sid}")
        else:
            _add_log(monitor_id, "⚠️ Twilio not configured")
            log.warning("ALERT: %s", message)
    except Exception as e:
        _add_log(monitor_id, f"❌ Failed: {str(e)[:60]}")


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
