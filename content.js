// ── BMS Seat Alert — Content Script ──────────────────────────────────────────
// Works on two page types:
//   1. /movies/{city}/seat-layout/{ec}/{vc}/{sid}/{date}  — seat map page
//   2. /movies/{city}/{slug}/buytickets/{ec}/{date}       — movie listing page

// ── Page type detection ───────────────────────────────────────────────────────
function detectPageType() {
  const path = window.location.pathname;
  if (/\/seat-layout\//.test(path)) return 'seat-layout';
  // Movie-level listing: /movies/{city}/{slug}/buytickets/{eventCode}/{date}
  // Note: cinema-specific pages are /cinemas/... — user won't land there directly
  if (/\/movies\/[^/]+\/[^/]+\/buytickets\//.test(path)) return 'listing';
  return 'other';
}

// ══════════════════════════════════════════════════════════════════════════════
//  SEAT-LAYOUT PAGE — parse the specific session being viewed
// ══════════════════════════════════════════════════════════════════════════════

function parseShowInfo() {
  const info = {
    bookingUrl: window.location.href,
    eventCode:  '',
    venueCode:  '',
    showId:     '',
    date:       '',
    movie:      '',
    venue:      '',
    showtime:   '',
  };

  // ── 1. URL parsing ─────────────────────────────────────────────────────────
  const urlMatch = window.location.pathname.match(
    /\/seat-layout\/([^/]+)\/([^/]+)\/([^/]+)\/([^/]+)/
  );
  if (urlMatch) {
    info.eventCode = urlMatch[1];
    info.venueCode = urlMatch[2];
    info.showId    = urlMatch[3];
    info.date      = urlMatch[4];
  }
  if (!info.date) {
    const dm = window.location.pathname.match(/\/(\d{8})(?:\/|$)/);
    if (dm) info.date = dm[1];
  }

  // ── 2. PRIMARY: window.__INITIAL_STATE__ ──────────────────────────────────
  safeExtract(() => {
    const sl = window.__INITIAL_STATE__?.seatlayoutMovies?.seatLayoutData;
    if (!sl) return;
    if (!info.movie && sl.eventName)               info.movie = sl.eventName;
    if (!info.venue && sl.currentVenue?.VenueName) info.venue = sl.currentVenue.VenueName;
    const shows = sl.currentVenue?.ShowTimes || [];
    const thisShow = shows.find(s => String(s.SessionId) === info.showId)
                  || sl.currentShowtime;
    if (thisShow?.ShowTime && !info.showtime)       info.showtime = thisShow.ShowTime;
  });

  // ── 3. DOM fallbacks ───────────────────────────────────────────────────────
  if (!info.movie) {
    info.movie = safeExtract(() => {
      const h1 = document.querySelector('h1');
      return h1 ? h1.textContent.trim().substring(0, 80) : '';
    });
  }
  if (!info.venue || !info.showtime) {
    safeExtract(() => {
      const spans = Array.from(document.querySelectorAll('span'));
      const infoSpan = spans.find(
        el => /\|/.test(el.textContent) && /\d{1,2}:\d{2}/.test(el.textContent)
      );
      if (!infoSpan) return;
      const parts = infoSpan.textContent.split('|').map(s => s.trim());
      if (!info.venue && parts[0])    info.venue = parts[0];
      const timePart = parts.find(p => /\d{1,2}:\d{2}\s*[AP]M/i.test(p));
      if (timePart && !info.showtime) {
        const m = timePart.match(/(\d{1,2}:\d{2}\s*[AP]M)/i);
        if (m) info.showtime = m[0].toUpperCase();
      }
    });
  }
  if (!info.movie && document.title && !document.title.includes('BookMyShow')) {
    const tp = document.title.split('|').map(s => s.trim()).filter(Boolean);
    if (tp[0]) info.movie = tp[0];
  }

  // ── 4. Append readable date label ─────────────────────────────────────────
  if (info.date && info.date.length === 8 && info.showtime) {
    info.showtime = safeExtract(() => {
      const y = info.date.slice(0, 4);
      const m = info.date.slice(4, 6);
      const d = info.date.slice(6, 8);
      const label = new Date(`${y}-${m}-${d}`)
        .toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
      return `${info.showtime}, ${label}`;
    }) || info.showtime;
  }

  return info;
}

// ══════════════════════════════════════════════════════════════════════════════
//  LISTING PAGE  (/movies/{city}/{slug}/buytickets/{eventCode}/{date})
//
//  The page renders a theatre card per cinema, each containing:
//    - An <a href="/cinemas/{city}/{slug}/buytickets/{venueCode}/{date}"> link
//    - Time <div>s with showtime text ("10:15 PM")
//
//  Strategy: per-theatre container scoping.
//  For each cinema link we walk UP the ancestor chain until we find the smallest
//  element that contains ONLY that one cinema link (not any sibling links).
//  All showtime leaf-nodes are then collected from WITHIN that container alone,
//  so we never accidentally grab showtimes from an adjacent theatre card.
//  Results are deduplicated by venueCode and showtimes are merged.
// ══════════════════════════════════════════════════════════════════════════════

function parseListingInfo() {
  const info = {
    movie:     '',
    eventCode: '',
    date:      '',
    theatres:  [],  // [{name, venueCode, date, cinemaUrl, showtimes:[string]}]
  };

  // ── eventCode + date from URL ─────────────────────────────────────────────
  const urlMatch = window.location.pathname.match(
    /\/movies\/[^/]+\/[^/]+\/buytickets\/(ET[^/]+)\/(\d{8})/i
  );
  if (urlMatch) {
    info.eventCode = urlMatch[1];
    info.date      = urlMatch[2];
  }
  if (!info.eventCode) {
    const m = window.location.pathname.match(/(ET\d+)/i);
    if (m) info.eventCode = m[1];
  }

  // ── Movie name — "Youth Movie Showtimes in Chennai…" → "Youth" ────────────
  info.movie = safeExtract(() => {
    const h1 = document.querySelector('h1');
    if (h1) return h1.textContent.trim().replace(/\s*[-–(].*$/, '').trim().substring(0, 80);
    return '';
  }) || '';
  if (!info.movie) {
    const m = document.title.match(/^(.+?)\s+(?:Movie\s+Showtimes|-|–|\|)/i);
    if (m) info.movie = m[1].trim().substring(0, 80);
    else {
      const tp = document.title.split(/[-|]/).map(s => s.trim())
        .filter(s => s && !s.includes('BookMyShow'));
      if (tp[0]) info.movie = tp[0].substring(0, 80);
    }
  }

  // ── Find all cinema links ─────────────────────────────────────────────────
  // href pattern: /cinemas/{city}/{cinema-slug}/buytickets/{venueCode}/{date}
  const cinemaLinks = Array.from(document.querySelectorAll(
    'a[href*="/cinemas/"][href*="/buytickets/"]'
  ));
  console.log(`[BMS] parseListingInfo: found ${cinemaLinks.length} cinema link(s)`);
  if (cinemaLinks.length === 0) return info;

  const timePattern = /^\s*\d{1,2}:\d{2}\s*(AM|PM)\s*$/i;

  // venueCode → theatre record  (for dedup + showtime merge)
  const venueMap = new Map();

  cinemaLinks.forEach((link, idx) => {
    const href = link.getAttribute('href') || '';
    const m = href.match(/\/cinemas\/([^/]+)\/([^/]+)\/buytickets\/([^/?#]+)\/(\d{8})/);
    if (!m) {
      console.log(`[BMS]   link[${idx}] no venueCode match in href="${href}", skipping`);
      return;
    }

    const [, , cinemaSlug, venueCode, date] = m;
    const cinemaUrl = href.startsWith('http')
      ? href
      : `https://in.bookmyshow.com${href}`;

    // ── Per-theatre container detection ──────────────────────────────────────
    // Walk UP ancestors until we reach an element that wraps EXACTLY this one
    // cinema link (not any other cinema link).  That's the theatre card.
    let container = link.parentElement;
    for (let depth = 0; depth < 15 && container && container !== document.body; depth++) {
      const linksInside = container.querySelectorAll(
        'a[href*="/cinemas/"][href*="/buytickets/"]'
      );
      if (linksInside.length === 1) break;   // found the single-theatre wrapper
      container = container.parentElement;
    }
    // Safety: if we walked all the way up without isolating, fall back to link's parent
    if (!container || container === document.body) container = link.parentElement;

    const containerDesc = container
      ? `${container.tagName}.${String(container.className).replace(/\s+/g, '.').slice(0, 50)}`
      : 'null';
    console.log(`[BMS]   link[${idx}] venueCode=${venueCode} container=${containerDesc}`);

    // ── Theatre name — scoped to this container ───────────────────────────
    let name = '';

    // 1. Anchor text (BMS sometimes wraps the cinema name inside the <a>)
    const linkText = (link.textContent || '').trim().replace(/\s+/g, ' ');
    if (linkText.length >= 4 && linkText.length <= 120 && !/^\d{1,2}:\d{2}/.test(linkText)) {
      name = linkText.substring(0, 80);
    }

    // 2. Heading / semantic-class element inside the container
    if (!name) {
      name = safeExtract(() => {
        const selectors = [
          'h2', 'h3', 'h4',
          '[class*="venue"]', '[class*="theatre"]', '[class*="cinema"]', '[class*="name"]',
        ];
        for (const sel of selectors) {
          for (const c of container.querySelectorAll(sel)) {
            if (c === link || c.contains(link)) continue;   // skip the anchor itself
            const t = c.textContent.trim().replace(/\s+/g, ' ');
            if (t.length >= 4 && t.length <= 120 && !/^\d{1,2}:\d{2}/.test(t)) {
              return t.substring(0, 80);
            }
          }
        }
        return '';
      }) || '';
    }

    // 3. Walk up from link looking for a heading sibling (stays inside container)
    if (!name) {
      name = safeExtract(() => {
        let el = link.parentElement;
        while (el && el !== container && el !== document.body) {
          for (const sel of ['h2', 'h3', 'h4']) {
            const h = el.querySelector(sel);
            if (h) {
              const t = h.textContent.trim().replace(/\s+/g, ' ');
              if (t.length >= 4 && t.length <= 120 && !/^\d{1,2}:\d{2}/.test(t)) {
                return t.substring(0, 80);
              }
            }
          }
          el = el.parentElement;
        }
        return '';
      }) || '';
    }

    // 4. Prettify the URL slug as last resort
    if (!name) {
      name = cinemaSlug
        .replace(/-/g, ' ')
        .replace(/\b\w/g, c => c.toUpperCase())
        .substring(0, 80);
    }

    // ── Showtimes — only from within this theatre's container ───────────────
    const showtimes = Array.from(container.querySelectorAll('*'))
      .filter(el => el.children.length === 0 && timePattern.test(el.textContent))
      .map(el => el.textContent.trim())
      .filter(Boolean);

    console.log(`[BMS]   link[${idx}] name="${name}" showtimes=${JSON.stringify(showtimes)}`);

    // ── Dedup by venueCode — merge showtimes if same venue appears twice ─────
    if (venueMap.has(venueCode)) {
      const existing = venueMap.get(venueCode);
      const merged = Array.from(new Set([...existing.showtimes, ...showtimes]));
      existing.showtimes = merged;
      console.log(`[BMS]   link[${idx}] venueCode=${venueCode} already seen — merged to ${merged.length} showtime(s)`);
    } else {
      venueMap.set(venueCode, { name, venueCode, date: date || info.date, cinemaUrl, showtimes });
    }
  });

  info.theatres = Array.from(venueMap.values());
  console.log(`[BMS] parseListingInfo done: ${info.theatres.length} unique theatre(s)`);
  return info;
}

// ── Utility ───────────────────────────────────────────────────────────────────
function safeExtract(fn) {
  try { return fn() || ''; } catch { return ''; }
}

// ── Message listener ──────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((req, sender, sendResponse) => {
  if (req.action === 'getShowInfo') {
    const pageType = detectPageType();

    if (pageType === 'listing') {
      // Retry up to 2 times if the first parse finds no showtimes —
      // BMS React pages sometimes finish hydrating a moment after DOMContentLoaded.
      const MAX_ATTEMPTS = 2;
      const RETRY_DELAY_MS = 500 + Math.floor(Math.random() * 300); // 500-800 ms

      const tryParse = (attempt) => {
        const info = parseListingInfo();
        const usable = info.theatres.some(t => t.showtimes.length > 0);
        console.log(
          `[BMS] listing parse attempt ${attempt}/${MAX_ATTEMPTS}: ` +
          `${info.theatres.length} theatre(s), usable=${usable}`
        );

        if (!usable && attempt < MAX_ATTEMPTS) {
          console.log(`[BMS] retrying parseListingInfo in ${RETRY_DELAY_MS}ms…`);
          setTimeout(() => tryParse(attempt + 1), RETRY_DELAY_MS);
        } else {
          sendResponse({ listing: usable ? info : null });
        }
      };

      tryParse(1);

    } else {
      const show = parseShowInfo();
      const hasState = !!window.__INITIAL_STATE__?.seatlayoutMovies?.seatLayoutData;
      const onShowPage = show.showId || show.movie || show.eventCode || hasState;
      sendResponse({ show: onShowPage ? show : null });
    }
  }
  return true; // keep channel open for async sendResponse
});
