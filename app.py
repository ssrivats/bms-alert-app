"""
BookMyShow Seat Alert backend.

The extension owns monitoring and row detection.
This backend stores monitor state and sends WhatsApp alerts on demand.
"""

import json
import logging
import os
import re
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
MAX_ACTIVE_PER_PHONE = 5

if not TWILIO_SID or not TWILIO_TOKEN:
    log.warning("TWILIO NOT CONFIGURED — WhatsApp alerts will not be sent")

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


def _append_log(monitor_id, message, event_type="info"):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return
    monitor.setdefault("logs", []).append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "type": event_type,
    })
    monitor["logs"] = monitor["logs"][-100:]
    _save_monitor(monitor_id, monitor)
    log.info("[%s] %s", monitor_id, message)


def _normalize_phone(phone):
    text = str(phone or "").strip()
    if not text:
      return ""
    if text.startswith("whatsapp:"):
        text = text.replace("whatsapp:", "", 1)
    if not text.startswith("+"):
        text = f"+91{text}"
    return text


def _normalize_row(value):
    text = str(value or "").strip().upper()
    if not text:
        return ""
    match = re.match(r"([A-Z]+[0-9]*|[0-9]+[A-Z]*)", text)
    return match.group(1) if match else ""


def _send_whatsapp(phone, body):
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    return client.messages.create(
        body=body,
        from_=TWILIO_FROM,
        to=f"whatsapp:{phone}",
    )


@app.route("/health")
def health():
    monitors = _load_all_monitors()
    active = sum(1 for monitor in monitors.values() if monitor.get("status") in {"monitoring", "checking_rows", "alert_failed"})
    return jsonify({
        "status": "ok",
        "active_monitors": active,
        "total_monitors": len(monitors),
        "redis": bool(_redis),
        "twilio": bool(TWILIO_SID and TWILIO_TOKEN),
        "time": datetime.now().isoformat(),
    })


@app.route("/api/monitor", methods=["POST"])
def create_monitor():
    data = request.json or {}
    phone = _normalize_phone(data.get("phone", ""))
    if not phone:
        return jsonify({"error": "phone required"}), 400

    active_for_phone = sum(
        1 for monitor in _load_all_monitors().values()
        if monitor.get("config", {}).get("phone") == phone
        and monitor.get("status") in {"monitoring", "checking_rows", "alert_failed"}
    )
    if active_for_phone >= MAX_ACTIVE_PER_PHONE:
        return jsonify({"error": f"You already have {active_for_phone} active monitors. Stop one first."}), 429

    monitor_id = str(uuid.uuid4())[:8]
    config = {
        "movie": str(data.get("movie", "")).strip(),
        "theatre": str(data.get("theatre", "")).strip(),
        "showtime": str(data.get("showtime", "")).strip(),
        "booking_url": str(data.get("booking_url", "")).strip(),
        "phone": phone,
        "poll_interval": max(60, int(data.get("poll_interval", 60))),
        "preferred_categories": sorted({
            str(code).strip().upper()
            for code in (data.get("preferred_categories") or [])
            if str(code).strip()
        }),
        "preferred_rows": sorted({
            _normalize_row(row)
            for row in (data.get("preferred_rows") or [])
            if _normalize_row(row)
        }),
    }

    monitor = {
        "id": monitor_id,
        "config": config,
        "status": "monitoring",
        "started_at": datetime.now().isoformat(),
        "last_checked": None,
        "last_result": None,
        "last_error": None,
        "alert_sent": False,
        "logs": [],
    }
    _save_monitor(monitor_id, monitor)
    _append_log(monitor_id, "Monitor created", "start")

    return jsonify({
        "monitor_id": monitor_id,
        "status": monitor["status"],
        "poll_interval": config["poll_interval"],
    })


@app.route("/api/monitor/<monitor_id>")
def get_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": monitor["id"],
        "status": monitor["status"],
        "started_at": monitor["started_at"],
        "last_checked": monitor.get("last_checked"),
        "last_result": monitor.get("last_result"),
        "last_error": monitor.get("last_error"),
        "alert_sent": monitor.get("alert_sent", False),
        "logs": monitor.get("logs", [])[-50:],
    })


@app.route("/api/monitor/<monitor_id>/update", methods=["POST"])
def update_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    status = str(data.get("status", monitor.get("status", ""))).strip() or monitor.get("status", "monitoring")
    message = str(data.get("message", "")).strip()

    monitor["status"] = status
    monitor["last_checked"] = datetime.now().strftime("%H:%M:%S")
    if message:
        monitor["last_result"] = message
        if status == "error":
            monitor["last_error"] = message
    if data.get("booking_url"):
        monitor["config"]["booking_url"] = str(data["booking_url"]).strip()
    _save_monitor(monitor_id, monitor)

    if message:
        event_type = "error" if status == "error" else "info"
        _append_log(monitor_id, message, event_type)

    return jsonify({"ok": True, "status": status})


@app.route("/api/monitor/<monitor_id>/alert", methods=["POST"])
def alert_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"ok": False, "message": "Monitor not found"}), 404

    if monitor.get("alert_sent"):
        return jsonify({"ok": True, "status": "alert_sent", "message": "Alert already sent"})

    if not TWILIO_SID or not TWILIO_TOKEN:
        monitor["status"] = "alert_failed"
        monitor["last_error"] = "Twilio credentials not configured"
        _save_monitor(monitor_id, monitor)
        _append_log(monitor_id, "Alert failed — Twilio not configured", "error")
        return jsonify({"ok": False, "status": "alert_failed", "message": "Twilio credentials not configured"}), 500

    config = monitor["config"]
    body = (
        "🚨 *Row Open on BookMyShow!*\n\n"
        f"🎬 {config.get('movie') or 'Your show'}\n"
    )
    if config.get("theatre"):
        body += f"🏠 {config['theatre']}\n"
    if config.get("showtime"):
        body += f"🕐 {config['showtime']}\n"
    if monitor.get("last_result"):
        body += f"💺 {monitor['last_result']}\n"
    if config.get("booking_url"):
        body += f"\n👉 Book now: {config['booking_url']}\n"
    body += f"\n_Alert at {datetime.now().strftime('%H:%M')}_"

    try:
        msg = _send_whatsapp(config["phone"], body)
        monitor["alert_sent"] = True
        monitor["status"] = "alert_sent"
        monitor["last_checked"] = datetime.now().strftime("%H:%M:%S")
        _save_monitor(monitor_id, monitor)
        _append_log(monitor_id, f"WhatsApp sent SID: {msg.sid}", "alert")
        return jsonify({"ok": True, "status": "alert_sent", "message": "Alert sent"})
    except Exception as exc:
        monitor["status"] = "alert_failed"
        monitor["last_error"] = str(exc)[:200]
        _save_monitor(monitor_id, monitor)
        _append_log(monitor_id, f"Alert failed: {str(exc)[:120]}", "error")
        return jsonify({"ok": False, "status": "alert_failed", "message": str(exc)[:200]}), 500


@app.route("/api/monitor/<monitor_id>/stop", methods=["POST"])
def stop_monitor(monitor_id):
    monitor = _load_monitor(monitor_id)
    if not monitor:
        return jsonify({"error": "Not found"}), 404
    monitor["status"] = "stopped"
    _save_monitor(monitor_id, monitor)
    _append_log(monitor_id, "Stopped by user", "stop")
    return jsonify({"status": "stopped"})


@app.route("/api/test-whatsapp", methods=["POST"])
def test_whatsapp():
    data = request.json or {}
    phone = _normalize_phone(data.get("phone", ""))
    if not phone:
        return jsonify({"error": "phone required"}), 400
    if not TWILIO_SID or not TWILIO_TOKEN:
        return jsonify({"error": "Twilio credentials not configured on server"}), 500

    msg = _send_whatsapp(
        phone,
        (
            "👋 *BMS Seat Alert — Test Message*\n\n"
            "✅ WhatsApp alerts are working.\n"
            "You will get a message like this when your selected rows open.\n\n"
            "_Powered by BMS Seat Alert_"
        ),
    )
    return jsonify({"status": "sent", "sid": msg.sid, "to": phone})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
