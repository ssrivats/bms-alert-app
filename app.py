"""
BookMyShow Seat Alert — Web App
================================
Flask backend that provides:
  - BMS data APIs (cities, movies, theatres, showtimes)
  - Monitoring engine (polls BMS for seat availability)
  - WhatsApp alerting via Twilio
"""

import os
import uuid
import time
import logging
import threading
from datetime import datetime

import requests as http_requests
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from twilio.rest import Client

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # Allow Chrome extension to call the API

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Twilio config (server-side only) ──────────────────────────────────────────
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM_NUMBER", "whatsapp:+14155238886")

# ── In-memory store for active monitors ───────────────────────────────────────
monitors = {}  # monitor_id -> { status, logs[], config, ... }

# ── BMS API helpers ───────────────────────────────────────────────────────────
BMS_BASE = "https://in.bookmyshow.com"
BMS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Popular cities (hardcoded for speed + reliability) ────────────────────────
CITIES = [
    {"code": "CHEN", "name": "Chennai"},
    {"code": "MUMBAI", "name": "Mumbai"},
    {"code": "BANG", "name": "Bengaluru"},
    {"code": "HYDB", "name": "Hyderabad"},
    {"code": "NCR", "name": "Delhi-NCR"},
    {"code": "KOLK", "name": "Kolkata"},
    {"code": "PUNE", "name": "Pune"},
    {"code": "AHMD", "name": "Ahmedabad"},
    {"code": "KOCH", "name": "Kochi"},
    {"code": "COIMB", "name": "Coimbatore"},
    {"code": "JAIPR", "name": "Jaipur"},
    {"code": "LUCK", "name": "Lucknow"},
    {"code": "CHND", "name": "Chandigarh"},
    {"code": "VIZAG", "name": "Visakhapatnam"},
    {"code": "MADU", "name": "Madurai"},
    {"code": "TRICH", "name": "Trichy"},
    {"code": "INDO", "name": "Indore"},
    {"code": "NAGP", "name": "Nagpur"},
    {"code": "VADO", "name": "Vadodara"},
    {"code": "SURT", "name": "Surat"},
]


# ══════════════════════════════════════════════════════════════════════════════
#  API ROUTES — BMS Data
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/cities")
def get_cities():
    return jsonify(CITIES)


# ── Movie cache (city_code -> { movies, fetched_at }) ─────────────────────────
_movie_cache = {}
_CACHE_TTL = 600  # 10 minutes


@app.route("/api/movies")
def search_movies():
    """
    Return all now-showing movies for a city.
    Uses Playwright to scrape BMS (bypasses API blocks / geo-restrictions).
    Results are cached for 10 minutes.
    Query params: city
    """
    city = request.args.get("city", "CHEN").upper()

    # Serve from cache if fresh
    cached = _movie_cache.get(city)
    if cached and (time.time() - cached["fetched_at"]) < _CACHE_TTL:
        log.info("Serving movies from cache for %s (%d movies)", city, len(cached["movies"]))
        return jsonify(cached["movies"])

    log.info("Fetching movies for %s via Playwright...", city)
    movies = _scrape_movies(city)

    _movie_cache[city] = {"movies": movies, "fetched_at": time.time()}
    return jsonify(movies)


def _scrape_movies(city_code: str) -> list:
    """Use Playwright to scrape the BMS explore page for now-showing movies."""
    from playwright.sync_api import sync_playwright

    city_slug = city_code.lower()
    url = f"{BMS_BASE}/explore/movies-{city_slug}"

    movies = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=BMS_HEADERS["User-Agent"])
            page.route("**/*.{png,jpg,jpeg,gif,woff,woff2,ttf,eot,mp4}", lambda r: r.abort())

            page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            # Extract movie data from the page
            movies = page.evaluate("""
                () => {
                    const results = [];
                    // Try multiple selectors BMS uses
                    const selectors = [
                        '[data-testid="movie-card"]',
                        '.movie-card-container',
                        '.__item',
                        '.bwc__item',
                        '[class*="MovieCard"]',
                        '[class*="movieCard"]',
                        'a[href*="/buytickets/"]',
                    ];

                    for (const sel of selectors) {
                        const cards = document.querySelectorAll(sel);
                        if (cards.length > 0) {
                            cards.forEach(card => {
                                const titleEl = card.querySelector('h3, h4, [class*="title"], [class*="Title"], strong');
                                const langEl = card.querySelector('[class*="language"], [class*="Language"], [class*="genre"], [class*="Genre"]');
                                const link = card.tagName === 'A' ? card : card.querySelector('a[href*="/buytickets/"]');
                                const href = link ? link.getAttribute('href') : '';

                                if (titleEl && titleEl.textContent.trim()) {
                                    // Extract event code from URL
                                    const match = href.match(/\\/buytickets\\/([^/]+)/);
                                    const id = match ? match[1] : '';
                                    results.push({
                                        id: id,
                                        title: titleEl.textContent.trim(),
                                        language: langEl ? langEl.textContent.trim() : '',
                                        genre: '',
                                        slug: href,
                                    });
                                }
                            });
                            if (results.length > 0) break;
                        }
                    }

                    // Deduplicate by title
                    const seen = new Set();
                    return results.filter(m => {
                        if (!m.title || seen.has(m.title)) return false;
                        seen.add(m.title);
                        return true;
                    });
                }
            """)

            browser.close()
            log.info("Scraped %d movies for %s", len(movies), city_code)

    except Exception as e:
        log.error("Movie scrape failed for %s: %s", city_code, e)

    return movies


@app.route("/api/showtimes")
def get_showtimes():
    """
    Get theatres + showtimes for a movie.
    Query params: city, movie_id, date (YYYYMMDD, optional)
    """
    city = request.args.get("city", "CHEN")
    movie_id = request.args.get("movie_id", "")
    date = request.args.get("date", datetime.now().strftime("%Y%m%d"))

    if not movie_id:
        return jsonify([])

    try:
        url = f"{BMS_BASE}/buytickets/{movie_id}/movie-{city.lower()}-{movie_id}/{date}"
        resp = http_requests.get(url, headers=BMS_HEADERS, timeout=10)

        # Try the showtime data API
        api_url = f"{BMS_BASE}/api/movies-data/showtimes-by-event"
        params = {
            "appCode": "MOBAND2",
            "appVersion": "14.7.7",
            "language": "en",
            "eventCode": movie_id,
            "regionCode": city,
            "subRegion": city,
            "bmsId": "",
            "isS498": "Y",
            "is498": "Y",
            "date": date,
        }
        resp = http_requests.get(api_url, headers=BMS_HEADERS, params=params, timeout=10)

        theatres = []
        if resp.status_code == 200:
            data = resp.json()
            venues = data.get("ShowDetails", [])
            if not venues:
                venues = data.get("venues", data.get("data", {}).get("venues", []))

            for venue in venues:
                shows = []
                show_list = venue.get("ShowTimes", venue.get("shows", []))
                for show in show_list:
                    shows.append({
                        "id": show.get("SessionId", show.get("id", "")),
                        "time": show.get("ShowTime", show.get("time", "")),
                        "screen": show.get("ScreenName", show.get("screen", "")),
                        "available": show.get("MaxSeats", 1) > 0,
                        "booking_url": show.get("BookingUrl", show.get("url", "")),
                        "categories": show.get("Categories", []),
                    })

                if shows:
                    theatres.append({
                        "name": venue.get("VenueName", venue.get("name", "")),
                        "code": venue.get("VenueCode", venue.get("code", "")),
                        "address": venue.get("VenueAddress", venue.get("address", "")),
                        "shows": shows,
                    })

        return jsonify(theatres)

    except Exception as e:
        log.error("Showtime fetch failed: %s", e)
        return jsonify([])


# ══════════════════════════════════════════════════════════════════════════════
#  API ROUTES — Monitoring
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/monitor", methods=["POST"])
def start_monitor():
    """Start monitoring a show for seat availability."""
    data = request.json
    monitor_id = str(uuid.uuid4())[:8]

    config = {
        "city": data.get("city", ""),
        "movie": data.get("movie", ""),
        "theatre": data.get("theatre", ""),
        "showtime": data.get("showtime", ""),
        "show_id": data.get("show_id", ""),
        "booking_url": data.get("booking_url", ""),
        "preferred_row": data.get("preferred_row", "").upper(),
        "preferred_seats": data.get("preferred_seats", ""),
        "phone": data.get("phone", ""),
        "poll_interval": int(data.get("poll_interval", 8)),
    }

    monitors[monitor_id] = {
        "id": monitor_id,
        "config": config,
        "status": "starting",
        "started_at": datetime.now().isoformat(),
        "poll_count": 0,
        "last_checked": None,
        "last_result": None,
        "alert_sent": False,
        "logs": [],
    }

    # Start monitoring in background thread
    thread = threading.Thread(
        target=_run_monitor,
        args=(monitor_id,),
        daemon=True,
    )
    thread.start()

    return jsonify({"monitor_id": monitor_id, "status": "started"})


@app.route("/api/monitor/<monitor_id>")
def get_monitor_status(monitor_id):
    """Get current status of a monitor."""
    monitor = monitors.get(monitor_id)
    if not monitor:
        return jsonify({"error": "Monitor not found"}), 404

    return jsonify({
        "id": monitor["id"],
        "status": monitor["status"],
        "poll_count": monitor["poll_count"],
        "last_checked": monitor["last_checked"],
        "last_result": monitor["last_result"],
        "alert_sent": monitor["alert_sent"],
        "config": {
            "movie": monitor["config"]["movie"],
            "theatre": monitor["config"]["theatre"],
            "showtime": monitor["config"]["showtime"],
            "preferred_row": monitor["config"]["preferred_row"],
        },
        "logs": monitor["logs"][-20:],  # Last 20 log entries
    })


@app.route("/api/monitor/<monitor_id>/stop", methods=["POST"])
def stop_monitor(monitor_id):
    """Stop a running monitor."""
    monitor = monitors.get(monitor_id)
    if not monitor:
        return jsonify({"error": "Monitor not found"}), 404

    monitor["status"] = "stopped"
    return jsonify({"status": "stopped"})


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _add_log(monitor_id, message):
    monitors[monitor_id]["logs"].append({
        "time": datetime.now().strftime("%I:%M:%S %p"),
        "message": message,
    })
    log.info("[%s] %s", monitor_id, message)


def _run_monitor(monitor_id):
    """Background monitoring loop using Playwright."""
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    monitor = monitors[monitor_id]
    config = monitor["config"]
    monitor["status"] = "monitoring"
    _add_log(monitor_id, f"Starting monitor for {config['movie']} at {config['theatre']}")

    # Build the URL to monitor
    show_url = config.get("booking_url", "")
    if not show_url:
        show_url = f"{BMS_BASE}/buytickets/{config['show_id']}"

    _add_log(monitor_id, f"Monitoring URL: {show_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=BMS_HEADERS["User-Agent"])
        page = context.new_page()

        # Block heavy assets
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot}", lambda r: r.abort())

        while monitor["status"] == "monitoring":
            monitor["poll_count"] += 1
            poll_num = monitor["poll_count"]

            try:
                _add_log(monitor_id, f"Poll #{poll_num} — loading page...")
                page.goto(show_url, timeout=30_000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

                available, reason = _detect_seats(page, config)
                monitor["last_checked"] = datetime.now().strftime("%I:%M:%S %p")
                monitor["last_result"] = reason

                if available:
                    _add_log(monitor_id, f"🎉 SEATS AVAILABLE: {reason}")
                    monitor["status"] = "seats_found"

                    # Send WhatsApp alert
                    _send_alert(monitor_id, show_url)
                    break
                else:
                    _add_log(monitor_id, f"⏳ Not yet — {reason}")

            except PwTimeout:
                _add_log(monitor_id, "⚠️ Page load timed out, retrying...")
            except Exception as e:
                _add_log(monitor_id, f"⚠️ Error: {str(e)[:100]}")

            if monitor["status"] == "monitoring":
                time.sleep(config["poll_interval"])

        browser.close()

    if monitor["status"] != "seats_found":
        _add_log(monitor_id, "Monitor stopped.")


def _detect_seats(page, config) -> tuple:
    """Detect if booking is open and preferred seats are available."""
    page_text = page.inner_text("body").lower()

    # Not yet open signals
    not_open = ["coming soon", "notify me", "booking opens", "advance booking"]
    for phrase in not_open:
        if phrase in page_text:
            return False, f"Not open yet ('{phrase}')"

    # Sold out signals
    sold_out = ["sold out", "housefull", "no seats available"]
    for phrase in sold_out:
        if phrase in page_text:
            return False, f"Sold out ('{phrase}')"

    # Booking open signals
    selectors = [
        "a[href*='buytickets']",
        "button.book-tickets-btn",
        "[class*='bookTickets']",
        "[data-testid='book-tickets']",
        "a.btnBook",
        "button:has-text('Book')",
    ]

    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                # Check for preferred row
                pref_row = config.get("preferred_row", "")
                if pref_row:
                    # Try to find seat map with the row
                    if f"row {pref_row.lower()}" in page_text or f'"{pref_row.lower()}"' in page_text:
                        return True, f"Row {pref_row} seats detected!"

                    # Check for specific seats
                    pref_seats = config.get("preferred_seats", "")
                    if pref_seats:
                        seats = [s.strip().lower() for s in pref_seats.split(",")]
                        found = [s for s in seats if s in page_text]
                        if found:
                            return True, f"Seats found: {', '.join(found)}"

                    # Booking open but row not confirmed
                    return True, f"Booking open (Row {pref_row} not confirmed — check manually)"

                return True, "Booking is open!"
        except Exception:
            pass

    # Check if there are any clickable show buttons
    try:
        links = page.query_selector_all("a, button")
        for link in links:
            try:
                text = link.inner_text().lower().strip()
                if text in ("book", "book now", "book tickets") and link.is_visible():
                    return True, f"Book button found: '{text}'"
            except Exception:
                pass
    except Exception:
        pass

    return False, "No booking signals yet"


def _send_alert(monitor_id, show_url):
    """Send WhatsApp alert via Twilio."""
    monitor = monitors[monitor_id]
    config = monitor["config"]
    phone = config["phone"]

    # Format phone for Twilio
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    if not phone.startswith("whatsapp:+"):
        phone = phone.replace("whatsapp:", "whatsapp:+91")

    message = (
        f"🎬 *Seats Available!*\n\n"
        f"🎥 {config['movie']}\n"
        f"🏠 {config['theatre']}\n"
        f"🕐 {config['showtime']}\n"
    )
    if config.get("preferred_row"):
        message += f"💺 Row {config['preferred_row']}"
        if config.get("preferred_seats"):
            message += f" — Seats {config['preferred_seats']}"
        message += "\n"

    message += f"\n👉 Book now: {show_url}\n"
    message += f"\n_Detected at {datetime.now().strftime('%I:%M:%S %p')}_"

    try:
        if TWILIO_SID and TWILIO_TOKEN:
            client = Client(TWILIO_SID, TWILIO_TOKEN)
            msg = client.messages.create(body=message, from_=TWILIO_FROM, to=phone)
            _add_log(monitor_id, f"✅ WhatsApp alert sent! SID: {msg.sid}")
            monitor["alert_sent"] = True
        else:
            _add_log(monitor_id, "⚠️ Twilio not configured — alert logged but not sent")
            log.warning("ALERT (no Twilio): %s", message)
    except Exception as e:
        _add_log(monitor_id, f"❌ WhatsApp send failed: {str(e)[:100]}")
        log.error("Twilio error: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
