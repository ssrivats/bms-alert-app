"""
BookMyShow Seat Alert backend.

Row-aware behavior:
- monitor one BookMyShow session at a time
- optionally filter by selected section / price tier
- optionally filter by selected row letters
- capture seat-layout payloads instead of scraping canvas DOM
"""

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from collections import defaultdict
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


# sync_playwright uses greenlets and is NOT thread-safe across OS threads.
# Each monitor thread creates and manages its own Playwright instance instead
# of sharing one — see _run_monitor for usage.


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
        and monitor.get("status") in ("monitoring", "waiting_for_session")
    )
    if active_for_phone >= MAX_ACTIVE_PER_PHONE:
        return jsonify({"error": f"You already have {active_for_phone} active monitors. Stop one first."}), 429

    mode = data.get("mode", "session")
    preferred_categories = [
        str(code).strip().upper()
        for code in data.get("preferred_categories", []) or []
        if str(code).strip()
    ]
    preferred_rows = sorted({
        _normalize_row_label(row)
        for row in data.get("preferred_rows", []) or []
        if _normalize_row_label(row)
    })

    monitor_id = str(uuid.uuid4())[:8]

    if mode == "listing":
        # ── Sold-out show monitoring: poll cinema-specific page for session open ──
        listing_url     = data.get("listing_url", "").strip()
        event_code      = data.get("event_code", "").strip()
        venue_code      = data.get("venue_code", "").strip()
        date            = data.get("date", "").strip()
        target_showtime = data.get("target_showtime", "").strip()

        if not listing_url or not venue_code:
            return jsonify({"error": "listing_url and venue_code are required for listing mode"}), 400

        config = {
            "mode":             "listing",
            "movie":            data.get("movie", ""),
            "theatre":          data.get("theatre", ""),
            "showtime":         target_showtime,   # for _smart_interval + display
            "listing_url":      listing_url,
            "event_code":       event_code,
            "venue_code":       venue_code,
            "date":             date,
            "target_showtime":  target_showtime,
            "booking_url":      "",                # filled in once session is found
            "phone":            phone,
            "poll_interval":    _smart_interval(target_showtime, int(data.get("poll_interval", 20))),
            "session_id":       "",
            "preferred_categories": preferred_categories,
            "preferred_rows":   preferred_rows,
        }
        initial_status = "waiting_for_session"

    else:
        # ── Normal seat-layout session monitoring ──────────────────────────────
        booking_url = data.get("booking_url", "")
        url_match   = re.search(r"/seat-layout/[^/]+/[^/]+/([^/]+)/", booking_url)
        session_id  = url_match.group(1) if url_match else str(data.get("show_id", "")).strip()

        config = {
            "mode":             "session",
            "movie":            data.get("movie", ""),
            "theatre":          data.get("theatre", ""),
            "showtime":         data.get("showtime", ""),
            "booking_url":      booking_url,
            "phone":            phone,
            "poll_interval":    _smart_interval(data.get("showtime", ""), int(data.get("poll_interval", 20))),
            "session_id":       session_id,
            "preferred_categories": preferred_categories,
            "preferred_rows":   preferred_rows,
        }
        initial_status = "starting"

    monitor = {
        "id":           monitor_id,
        "config":       config,
        "status":       initial_status,
        "started_at":   datetime.now().isoformat(),
        "poll_count":   0,
        "last_checked": None,
        "last_result":  None,
        "last_error":   None,
        "alert_sent":   False,
        "failures":     0,
        "logs":         [],
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
            "session_id":   m["config"].get("session_id", ""),
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
    # sync_playwright uses greenlets and is NOT safe to share across OS threads.
    # Each monitor thread creates and owns its own Playwright + browser instance.
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    monitor = _load_monitor(monitor_id)
    config  = monitor["config"]
    mode    = config.get("mode", "session")

    # listing mode: status was already set to waiting_for_session in start_monitor
    # session mode: transition to monitoring now
    if mode != "listing":
        monitor["status"] = "monitoring"
        _save_monitor(monitor_id, monitor)

    _add_log(monitor_id, f"Started — watching {config['movie']}", "start")

    # Validate required URL
    start_url = config.get("listing_url") if mode == "listing" else config.get("booking_url")
    if not start_url:
        monitor = _load_monitor(monitor_id)
        monitor["status"] = "error"
        monitor["last_error"] = "No URL configured"
        _save_monitor(monitor_id, monitor)
        return

    start_time       = time.time()
    first_load       = True
    needs_navigation = False   # set True when transitioning listing → session

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                user_agent=BMS_UA,
                viewport={"width": 1280, "height": 900},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = context.new_page()
            payload_cache = {"snapshot": None, "source": "", "updated_at": None}
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)
            page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,ico}", lambda route: route.abort())
            page.on("response", lambda response: _capture_layout_response(response, payload_cache))

            while True:
                monitor = _load_monitor(monitor_id)
                if not monitor or monitor["status"] not in ("monitoring", "waiting_for_session"):
                    break

                if time.time() - start_time > MAX_RUNTIME_SECS:
                    monitor["status"] = "timeout"
                    _save_monitor(monitor_id, monitor)
                    _add_log(monitor_id, "⏰ Monitor timed out after 30 minutes", "timeout")
                    break

                # Always read fresh config (may have changed during a mode transition)
                config        = monitor["config"]
                mode          = config.get("mode", "session")
                showtime_hint = config.get("showtime") or config.get("target_showtime", "")
                poll_interval = _smart_interval(showtime_hint, config["poll_interval"])

                monitor["poll_count"] = monitor.get("poll_count", 0) + 1
                _save_monitor(monitor_id, monitor)
                _add_log(monitor_id, f"Poll #{monitor['poll_count']} (every {poll_interval}s)…", "poll")

                try:
                    # ── Navigate or reload ────────────────────────────────────
                    if first_load or needs_navigation:
                        nav_url = (
                            config.get("listing_url") if mode == "listing"
                            else config.get("booking_url")
                        )
                        wait_event = "domcontentloaded" if mode == "listing" else "networkidle"
                        page.goto(nav_url, timeout=45_000, wait_until=wait_event)
                        first_load       = False
                        needs_navigation = False
                    else:
                        wait_event = "domcontentloaded" if mode == "listing" else "networkidle"
                        page.reload(timeout=45_000, wait_until=wait_event)

                    page.wait_for_timeout(2000)

                    # For session mode wait until __INITIAL_STATE__ is hydrated
                    # (BMS populates it via client-side JS after domcontentloaded)
                    if mode == "session":
                        try:
                            page.wait_for_function(
                                "() => !!window.__INITIAL_STATE__?.seatlayoutMovies?.seatLayoutData",
                                timeout=20_000,
                            )
                        except Exception:
                            # Timeout or error — let _check_availability report details
                            pass

                    # ── Check availability (mode-specific) ───────────────────
                    if mode == "listing":
                        available, reason, session_id = _check_listing_availability(
                            page, config, monitor_id
                        )
                    else:
                        available, reason = _check_availability(
                            page,
                            config.get("session_id", ""),
                            config.get("preferred_categories", []),
                            config.get("preferred_rows", []),
                            payload_cache,
                            monitor_id=monitor_id,
                        )
                        session_id = None

                    monitor = _load_monitor(monitor_id)
                    monitor["last_checked"] = datetime.now().strftime("%H:%M:%S")
                    monitor["last_result"]  = reason
                    monitor["failures"]     = 0
                    _save_monitor(monitor_id, monitor)

                    # ── Handle result ─────────────────────────────────────────
                    if available:
                        if mode == "listing":
                            seat_url       = _build_seat_layout_url(config, session_id)
                            preferred_rows = config.get("preferred_rows", [])

                            if not preferred_rows:
                                # No row filter → alert immediately
                                monitor = _load_monitor(monitor_id)
                                monitor["status"] = "seats_found"
                                _save_monitor(monitor_id, monitor)
                                _add_log(monitor_id, f"✅ Show is open: {reason}", "found")
                                _send_alert(monitor_id, seat_url)
                                break

                            else:
                                # Row filter → transition to session mode
                                _add_log(
                                    monitor_id,
                                    f"🔄 Show opened — switching to row-level monitoring ({reason})",
                                    "info",
                                )
                                monitor = _load_monitor(monitor_id)
                                monitor["config"]["mode"]        = "session"
                                monitor["config"]["session_id"]  = session_id
                                monitor["config"]["booking_url"] = seat_url
                                monitor["status"] = "monitoring"
                                _save_monitor(monitor_id, monitor)
                                payload_cache    = {"snapshot": None, "source": "", "updated_at": None}
                                needs_navigation = True
                                continue  # navigate immediately, skip sleep

                        else:
                            monitor = _load_monitor(monitor_id)
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
            # browser and playwright are cleaned up by the `with` block

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


def _normalize_row_label(value):
    text = str(value or "").strip().upper()
    if not text:
        return ""
    match = re.match(r"([A-Z]+[0-9]*|[0-9]+[A-Z]*)", text)
    return match.group(1) if match else ""


def _normalize_section_code(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


def _walk_nodes(node, depth=0, seen=None):
    if seen is None:
        seen = set()
    if depth > 12:
        return
    if isinstance(node, dict):
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        yield node
        for value in node.values():
            yield from _walk_nodes(value, depth + 1, seen)
    elif isinstance(node, list):
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        for item in node:
            yield from _walk_nodes(item, depth + 1, seen)


def _find_render_data(node):
    if isinstance(node, dict) and isinstance(node.get("renderGroups"), list):
        return node

    for candidate in _walk_nodes(node):
        if isinstance(candidate.get("renderGroups"), list):
            return candidate
    return None


def _extract_layout_snapshot(node):
    render_data = _find_render_data(node)
    if not render_data:
        return None

    all_rows = set()
    sections = {}
    available_seats = []

    for group in render_data.get("renderGroups") or []:
        group_section = group.get("currentSeatArea") if isinstance(group, dict) else {}
        seats = group.get("seats") if isinstance(group, dict) else None
        if not isinstance(seats, list):
            continue

        for entry in seats:
            if not isinstance(entry, dict):
                continue

            seat = entry.get("seat") if isinstance(entry.get("seat"), dict) else entry
            merged = {}
            merged.update(group if isinstance(group, dict) else {})
            merged.update(entry)
            if isinstance(seat, dict):
                merged.update(seat)

            row = _normalize_row_label(
                merged.get("rowNumber")
                or merged.get("rowNo")
                or merged.get("rowLabel")
                or merged.get("rowId")
            )
            if not row:
                continue

            seat_type = str(
                merged.get("seatType")
                or merged.get("type")
                or merged.get("seatStatus")
                or ""
            ).strip()
            if "GANGWAY" in seat_type.upper() or "SPACE" in seat_type.upper():
                continue

            all_rows.add(row)

            area = merged.get("currentSeatArea") if isinstance(merged.get("currentSeatArea"), dict) else {}
            if not area and isinstance(group_section, dict):
                area = group_section

            section_code = _normalize_section_code(
                area.get("areaCode")
                or merged.get("areaCode")
                or area.get("areaId")
                or merged.get("areaId")
                or merged.get("PriceCode")
            )
            section_label = (
                area.get("areaDesc")
                or area.get("areaName")
                or area.get("name")
                or merged.get("priceDescription")
                or merged.get("PriceDesc")
                or section_code
                or "Unknown"
            )
            if section_code:
                sections[section_code] = str(section_label).strip() or section_code

            if _seat_is_available(merged):
                available_seats.append({
                    "row": row,
                    "section_code": section_code,
                    "section_label": str(section_label).strip() or section_code or "Unknown",
                    "seat_number": str(
                        merged.get("seatNumber")
                        or merged.get("actualSeatNo")
                        or merged.get("cinemaSeatNumber")
                        or merged.get("seatNo")
                        or merged.get("seatId")
                        or ""
                    ).strip(),
                })

    if not all_rows and not available_seats:
        return None

    return {
        "rows": sorted(all_rows),
        "sections": [{"code": code, "label": label} for code, label in sections.items()],
        "available_seats": available_seats,
    }


def _seat_is_available(seat):
    if not isinstance(seat, dict):
        return False

    sold = bool(seat.get("isSold") or seat.get("sold"))
    blocked = bool(seat.get("isBlocked") or seat.get("blocked"))
    if sold or blocked:
        return False

    for key in ("isAvailable", "available", "isOpen"):
        if seat.get(key) is True:
            return True

    status_values = [
        seat.get("seatStatus"),
        seat.get("status"),
        seat.get("seatType"),
        seat.get("availability"),
    ]
    text = " ".join(str(value or "").strip().lower() for value in status_values if value not in (None, ""))
    if any(token in text for token in ("sold", "booked", "blocked", "unavailable", "not available")):
        return False
    if any(token in text for token in ("available", "vacant", "open")):
        return True

    numeric_values = [
        seat.get("seatStatus"),
        seat.get("status"),
        seat.get("availabilityStatus"),
    ]
    for value in numeric_values:
        if str(value).strip() == "1":
            return True
        if str(value).strip() == "0":
            return False

    return False


def _capture_layout_response(response, payload_cache):
    try:
        headers = response.headers or {}
        content_type = headers.get("content-type", "").lower()
        url = response.url.lower()

        # Filter: only look at JSON responses or URLs that smell like seat data
        is_json = "json" in content_type
        is_seat_url = any(key in url for key in ("seat", "layout", "render", "showtime", "seatlayout"))
        if not is_json and not is_seat_url:
            return

        # Log every JSON endpoint we inspect so we can see what BMS is calling
        short_url = response.url[:120]
        log.info("[intercept] Checking response: %s (json=%s seat_url=%s)", short_url, is_json, is_seat_url)

        text = response.text()
        if not text or text[:1] not in "{[":
            return

        data = json.loads(text)
        snapshot = _extract_layout_snapshot(data)

        if snapshot:
            n_available = len(snapshot.get("available_seats", []))
            n_rows      = len(snapshot.get("rows", []))
            n_sections  = len(snapshot.get("sections", []))
            payload_cache["snapshot"]   = snapshot
            payload_cache["source"]     = response.url
            payload_cache["updated_at"] = time.time()
            log.info(
                "[intercept] ✅ Layout snapshot captured from %s — "
                "%d available seats | %d rows (%s) | %d sections (%s)",
                short_url,
                n_available,
                n_rows, ", ".join(snapshot.get("rows", [])[:10]),
                n_sections, ", ".join(s["label"] for s in snapshot.get("sections", [])[:5]),
            )
        else:
            # Log that we saw a JSON response but it had no renderGroups
            log.debug("[intercept] No renderGroups in response from %s", short_url)
    except Exception as exc:
        log.debug("[intercept] Error processing response: %s", exc)
        return


def _summarize_matches(matches):
    grouped = defaultdict(set)
    for seat in matches:
        label = seat.get("section_label") or seat.get("section_code") or "Unknown"
        grouped[label].add(seat.get("row") or "?")

    parts = []
    for label, rows in sorted(grouped.items()):
        parts.append(f"{label} rows {', '.join(sorted(rows))}")
    return "; ".join(parts)


def _check_availability(page, session_id, preferred_categories, preferred_rows, payload_cache, monitor_id=None):
    preferred_categories = [code.upper() for code in preferred_categories or []]
    preferred_rows = [_normalize_row_label(row) for row in preferred_rows or [] if _normalize_row_label(row)]

    try:
        state_data = page.evaluate("""
        (sessionId) => {
            function findRenderData(root) {
                const seen = new WeakSet();
                function walk(node, depth) {
                    if (!node || depth > 10) return null;
                    if (typeof node !== 'object') return null;
                    if (seen.has(node)) return null;
                    seen.add(node);

                    if (Array.isArray(node.renderGroups)) {
                        return node;
                    }

                    if (Array.isArray(node)) {
                        for (const item of node) {
                            const found = walk(item, depth + 1);
                            if (found) return found;
                        }
                        return null;
                    }

                    for (const value of Object.values(node)) {
                        const found = walk(value, depth + 1);
                        if (found) return found;
                    }
                    return null;
                }
                return walk(root, 0);
            }

            try {
                const initState = window.__INITIAL_STATE__;
                if (!initState) return { error: "no_initial_state:__INITIAL_STATE__missing" };
                const seatLayout = initState?.seatlayoutMovies?.seatLayoutData;
                if (!seatLayout) {
                    const keys = Object.keys(initState).join(",");
                    return { error: "no_initial_state:keys=" + keys.slice(0, 200) };
                }

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
                    layoutData: findRenderData(seatLayout),
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

    layout_snapshot = _extract_layout_snapshot(state_data.get("layoutData")) or payload_cache.get("snapshot")
    categories  = state_data.get("categories", [])
    show_time   = state_data.get("showTime", "?")
    matching = [
        category for category in categories
        if category.get("availStatus") == "1" and category.get("range")
    ]

    # ── Log snapshot diagnostics every poll ───────────────────────────────────
    if layout_snapshot:
        n_avail = len(layout_snapshot.get("available_seats", []))
        rows_present = layout_snapshot.get("rows", [])
        src = payload_cache.get("source", "inline")
        log.info("[avail] Layout snapshot: %d available seats | rows=%s | src=%s",
                 n_avail, rows_present[:8], src[:80])
    else:
        age_secs = time.time() - (payload_cache.get("updated_at") or 0)
        log.info("[avail] No layout snapshot yet (last update %.0fs ago, source=%s)",
                 age_secs if payload_cache.get("updated_at") else -1,
                 payload_cache.get("source", "none"))

    if preferred_rows:
        if not layout_snapshot:
            # Track how many consecutive polls have had no row data
            payload_cache["row_miss_count"] = payload_cache.get("row_miss_count", 0) + 1
            miss = payload_cache["row_miss_count"]

            # After 5 consecutive misses log a prominent warning
            if miss == 5:
                log.warning(
                    "[avail] Row data missing for %d consecutive polls — "
                    "BMS seat-layout API may not be firing on reload. "
                    "Falling back to category-level detection.", miss
                )
                if monitor_id:
                    _add_log(monitor_id,
                             f"⚠️ Row layout API not responding after {miss} polls — "
                             "switching to category-level detection",
                             "warn")

            # After 5 misses fall back to category-level so the monitor still fires
            if miss >= 5:
                if matching:
                    cat_summary = ", ".join(f"{c['label']} ₹{c['price']}" for c in matching)
                    return True, f"Seats open (category fallback): {cat_summary} ({show_time})"
                return False, f"All categories sold — row layout unavailable after {miss} polls ({show_time})"

            return False, f"Row layout not yet captured (poll {miss}/5 — waiting for BMS API)"

        # Reset miss counter once we have data
        payload_cache["row_miss_count"] = 0

        matches = [
            seat for seat in layout_snapshot.get("available_seats", [])
            if seat.get("row") in preferred_rows
            and (
                not preferred_categories
                or seat.get("section_code", "").upper() in preferred_categories
            )
        ]

        if matches:
            return True, f"Selected rows open: {_summarize_matches(matches)} ({show_time})"

        row_text = ", ".join(preferred_rows)
        if preferred_categories:
            labels = [
                category.get("label") or category.get("code")
                for category in categories
                if category.get("code", "").upper() in preferred_categories
            ]
            section_text = ", ".join(labels or preferred_categories)
            return False, f"Rows {row_text} still unavailable in {section_text} ({show_time})"

        return False, f"Rows {row_text} still unavailable ({show_time})"

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


def _build_seat_layout_url(config, session_id):
    """
    Construct a seat-layout URL from listing-mode config + discovered session_id.

    listing_url shape:  https://in.bookmyshow.com/cinemas/{city}/{cinema-slug}/buytickets/{venueCode}/{date}
    seat-layout shape:  https://in.bookmyshow.com/movies/{city}/seat-layout/{eventCode}/{venueCode}/{sessionId}/{date}
    """
    listing_url = config.get("listing_url", "")
    m = re.search(r"/cinemas/([^/]+)/", listing_url)
    city = m.group(1) if m else "chennai"
    event_code = config.get("event_code", "")
    venue_code = config.get("venue_code", "")
    date = config.get("date", "")
    return (
        f"https://in.bookmyshow.com/movies/{city}/seat-layout"
        f"/{event_code}/{venue_code}/{session_id}/{date}"
    )


def _check_listing_availability(page, config, monitor_id):
    """
    Read venueShowtimesFunctionalApi from __INITIAL_STATE__ on the cinema-specific
    buytickets page, find the target showtime, and return:
        (available: bool, reason: str, session_id: str | None)
    """
    venue_code      = config.get("venue_code", "")
    date            = config.get("date", "")
    target_showtime = config.get("target_showtime", "")
    event_code      = config.get("event_code", "")

    try:
        result = page.evaluate(
            """
            (args) => {
                const { venueCode, dateCode, targetShowtime, eventCode } = args;

                function norm(t) {
                    if (!t) return '';
                    return t.replace(/\\s+/g, ' ').toUpperCase()
                             .replace(/([0-9])(AM|PM)/, '$1 $2').trim();
                }

                try {
                    const api = window.__INITIAL_STATE__?.venueShowtimesFunctionalApi;
                    if (!api) return { error: 'no_venueShowtimesFunctionalApi' };

                    const queryKey = 'getShowtimesByVenue-' + venueCode + '-' + dateCode;
                    const query    = api.queries?.[queryKey];
                    if (!query) {
                        const keys = Object.keys(api.queries || {}).slice(0, 5);
                        return { error: 'query_key_not_found', queryKey, available_keys: keys };
                    }

                    const events  = query?.data?.showDetailsTransformed?.Event || [];
                    const matches = [];
                    const targetNorm = norm(targetShowtime);

                    for (const ev of events) {
                        const evCode = String(ev.EventCode || '');
                        if (eventCode && evCode !== eventCode) continue;

                        for (const child of ev.ChildEvents || []) {
                            for (const show of child.ShowTimes || []) {
                                if (norm(show.ShowTime) === targetNorm) {
                                    matches.push({
                                        sessionId:    String(show.SessionId   || ''),
                                        showTime:     show.ShowTime,
                                        availStatus:  String(show.AvailStatus || ''),
                                        showDateCode: String(show.ShowDateCode || ''),
                                        eventCode:    evCode,
                                    });
                                }
                            }
                        }
                    }

                    if (matches.length === 0) {
                        const all = [];
                        for (const ev of events) {
                            for (const child of ev.ChildEvents || []) {
                                for (const show of child.ShowTimes || []) {
                                    all.push({
                                        showTime:    show.ShowTime,
                                        availStatus: show.AvailStatus,
                                        sessionId:   show.SessionId,
                                    });
                                }
                            }
                        }
                        return { error: 'no_matching_showtime', targetShowtime, targetNorm, allShowtimes: all };
                    }

                    return { matches };
                } catch (e) {
                    return { error: 'js_exception: ' + e.message };
                }
            }
            """,
            {
                "venueCode":      venue_code,
                "dateCode":       date,
                "targetShowtime": target_showtime,
                "eventCode":      event_code,
            },
        )
    except Exception as exc:
        return False, f"JS evaluation failed: {str(exc)[:80]}", None

    if result.get("error"):
        err = result["error"]
        if err == "no_venueShowtimesFunctionalApi":
            return False, "Waiting — venue data not yet in page state", None
        if err == "query_key_not_found":
            keys_str = ", ".join(result.get("available_keys", []))
            _add_log(monitor_id, f"⚠️ Query key not found ({result.get('queryKey')}), saw: {keys_str}", "warn")
            return False, "Waiting — venue query key not found", None
        if err == "no_matching_showtime":
            found = [s["showTime"] for s in result.get("allShowtimes", [])]
            target = result.get("targetShowtime", target_showtime)
            _add_log(monitor_id, f"⚠️ Showtime '{target}' not matched. Found: {found[:6]}", "warn")
            return False, f"Showtime '{target}' not found in venue schedule", None
        return False, f"Page state error ({err})", None

    matches = result.get("matches", [])
    if not matches:
        return False, "No matching session found", None

    # Use first match (there should only be one for a given venue+time)
    show = matches[0]
    avail = show.get("availStatus", "0")
    show_time = show.get("showTime", target_showtime)
    session_id = show.get("sessionId", "")

    if avail == "1":
        return True, f"Show is open ({show_time})", session_id

    return False, f"Show still sold out ({show_time})", None


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


def _resume_monitors():
    """
    On startup, re-spawn threads for any monitors that were still active when
    the server last stopped (e.g. after a Railway redeploy).  Without this,
    Redis correctly persists the monitor records but nothing is actually polling.
    """
    if not _redis:
        return  # in-memory store is always empty on startup — nothing to resume

    resumed = 0
    for monitor in _load_all_monitors().values():
        status = monitor.get("status", "")
        if status not in ("monitoring", "waiting_for_session"):
            continue

        monitor_id = monitor["id"]
        # Reset failure count so we get a clean slate on the new thread
        monitor["failures"] = 0
        _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, "🔄 Resuming after server restart", "info")

        thread = threading.Thread(target=_run_monitor, args=(monitor_id,), daemon=True)
        thread.start()
        resumed += 1

    if resumed:
        log.info("▶ Resumed %d active monitor(s) from Redis", resumed)


# Resume any active monitors that survived in Redis across this restart
_resume_monitors()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
