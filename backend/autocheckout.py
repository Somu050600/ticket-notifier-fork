"""
autocheckout.py — BookMyShow & District headless auto-cart / checkout.

BookMyShow flow:
  buytickets URL → "How many seats?" popup → select qty → Continue
  → stadium map → select cheapest category → select available seats
  → Book → capture ticket-options URL → send to user

District flow:
  event page → select tier → add to cart → capture URL

Anti-detection:
  • Stealth init scripts (webdriver, plugins, chrome, WebGL fingerprints)
  • Human-like mouse movements and random delays
  • Realistic viewport / UA / locale / timezone
  • No automation flags exposed

Card pool (for full-checkout mode only):
  CARD_1_NUMBER / CARD_1_EXPIRY / CARD_1_CVV / CARD_1_NAME  ← highest priority
  CARD_2_NUMBER / CARD_2_EXPIRY / CARD_2_CVV / CARD_2_NAME
  CARD_3_NUMBER / CARD_3_EXPIRY / CARD_3_CVV / CARD_3_NAME
"""

import asyncio
import logging
import os
import queue
import random
import re
import threading
import time
from typing import Optional

logger = logging.getLogger("ticketalert.checkout")

# ── Card pool ─────────────────────────────────────────────────────────────────

def _load_card_pool() -> list[dict]:
    pool = []
    for n in range(1, 4):
        number = (
            os.environ.get(f"CARD_{n}_NUMBER") or
            (os.environ.get("CARD_NUMBER") if n == 1 else "")
        )
        expiry = (
            os.environ.get(f"CARD_{n}_EXPIRY") or
            (os.environ.get("CARD_EXPIRY") if n == 1 else "")
        )
        cvv = (
            os.environ.get(f"CARD_{n}_CVV") or
            (os.environ.get("CARD_CVV") if n == 1 else "")
        )
        name = (
            os.environ.get(f"CARD_{n}_NAME") or
            os.environ.get("PROFILE_NAME", "")
        )
        if number:
            pool.append({
                "priority": n, "number": number,
                "expiry": expiry, "cvv": cvv, "name": name,
            })
    return pool


def _profile() -> dict:
    return {
        "name":  os.environ.get("PROFILE_NAME",  ""),
        "email": os.environ.get("PROFILE_EMAIL", ""),
        "phone": os.environ.get("PROFILE_PHONE", ""),
    }


# ── Session state ─────────────────────────────────────────────────────────────
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_checkout_queue: "queue.Queue[tuple[str, str, dict, bool, str, str]]" = queue.Queue()
_checkout_workers: list[threading.Thread] = []
_checkout_worker_lock = threading.Lock()


def _session_id(watcher_id: str, priority: int) -> str:
    return f"{watcher_id}-slot-{priority}"


# ── Public API (called from app.py) ──────────────────────────────────────────

def claim_slot(watcher_id: str, device_id: str) -> Optional[str]:
    with _sessions_lock:
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("status") in ("running", "otp_required", "cart_ready") \
                    and sess.get("device_id") is None:
                sess["device_id"] = device_id
                logger.info(f"[{sid}] Claimed by device {device_id}")
                return sid
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return sid
    return None


def get_session(session_id: str) -> dict:
    """Returns session state including cart_url for frontend polling."""
    with _sessions_lock:
        sess = _sessions.get(session_id, {})
    return {
        "status":        sess.get("status", "idle"),
        "message":       sess.get("message", ""),
        "card_priority": sess.get("card_priority", 0),
        "device_id":     sess.get("device_id"),
        "cart_url":      sess.get("cart_url"),        # ← CRITICAL: expose to frontend
    }


def get_session_for_device(watcher_id: str, device_id: str) -> dict:
    with _sessions_lock:
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return {**get_session(sid), "session_id": sid}
    return {"status": "idle", "message": "", "session_id": None}


def inject_otp(session_id: str, otp: str):
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id]["otp"] = otp
            logger.info(f"[{session_id}] OTP injected")


def _update(session_id: str, **kwargs):
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id].update(kwargs)


def _is_actionable_cart_url(url: str) -> bool:
    if not url:
        return False
    return any(part in url.lower() for part in ("ticket-options", "cart", "checkout", "payment", "order", "booking"))


def _checkout_worker_loop():
    while True:
        job = _checkout_queue.get()
        if job is None:
            _checkout_queue.task_done()
            break
        try:
            _run_in_thread(*job)
        except Exception as e:
            logger.exception(f"Checkout worker crashed: {e}")
        finally:
            _checkout_queue.task_done()


def start_checkout_workers():
    desired = max(1, int(os.environ.get("CHECKOUT_WORKERS", "1")))
    with _checkout_worker_lock:
        alive = [t for t in _checkout_workers if t.is_alive()]
        _checkout_workers[:] = alive
        while len(_checkout_workers) < desired:
            idx = len(_checkout_workers) + 1
            t = threading.Thread(
                target=_checkout_worker_loop,
                name=f"checkout-worker-{idx}",
                daemon=True,
            )
            t.start()
            _checkout_workers.append(t)
    logger.info(
        "Checkout worker pool ready (%s worker%s)",
        len(_checkout_workers),
        "" if len(_checkout_workers) == 1 else "s",
    )


def _enqueue_checkout(session_id: str, checkout_url: str, card: dict,
                      cart_mode: bool, target_price: str, watcher_id: str):
    start_checkout_workers()
    _checkout_queue.put((session_id, checkout_url, card, cart_mode, target_price, watcher_id))


# ── Stealth & Anti-detection ──────────────────────────────────────────────────

STEALTH_JS = """
    // ── webdriver flag ──
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    try { delete navigator.__proto__.webdriver; } catch(e) {}

    // ── Realistic plugins ──
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',            description: 'Portable Document Format', length: 1 },
                { name: 'Chrome PDF Viewer',   filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',                         length: 1 },
                { name: 'Native Client',       filename: 'internal-nacl-plugin',           description: '',                         length: 2 },
            ];
            p.namedItem = (name) => p.find(x => x.name === name) || null;
            p.refresh = () => {};
            return p;
        }
    });

    // ── Languages ──
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-IN', 'en-US', 'en', 'hi']
    });

    // ── Chrome runtime ──
    window.chrome = {
        runtime: { connect: () => {}, sendMessage: () => {} },
        loadTimes: () => ({}),
        csi: () => ({})
    };

    // ── Permission query ──
    const _origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(params);

    // ── WebGL vendor/renderer ──
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return _getParam.call(this, p);
    };

    // ── Iframe contentWindow ──
    try {
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                return new Proxy(this._contentWindow || window, {
                    get: (target, key) => {
                        if (key === 'chrome') return window.chrome;
                        return Reflect.get(target, key);
                    }
                });
            }
        });
    } catch(e) {}
"""

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]


# ── Human-like helpers ────────────────────────────────────────────────────────

async def _human_delay(lo=0.3, hi=1.0):
    await asyncio.sleep(random.uniform(lo, hi))


async def _human_move(page, x, y):
    """Move mouse to (x,y) with human-like curve."""
    try:
        await page.mouse.move(x, y, steps=random.randint(8, 20))
    except Exception:
        pass


async def _human_click(page, locator_or_sel, timeout=6_000) -> bool:
    """Click with realistic mouse movement and jitter."""
    try:
        el = locator_or_sel if hasattr(locator_or_sel, 'click') else page.locator(locator_or_sel).first
        await el.wait_for(state="visible", timeout=timeout)
        box = await el.bounding_box()
        if box:
            x = box['x'] + random.uniform(box['width'] * 0.25, box['width'] * 0.75)
            y = box['y'] + random.uniform(box['height'] * 0.25, box['height'] * 0.75)
            await _human_move(page, x, y)
            await _human_delay(0.08, 0.25)
            await page.mouse.click(x, y)
        else:
            await el.click()
        await _human_delay(0.15, 0.4)
        return True
    except Exception:
        return False


async def _human_scroll(page, distance=400):
    """Scroll smoothly."""
    steps = random.randint(3, 7)
    for _ in range(steps):
        await page.mouse.wheel(0, distance // steps + random.randint(-15, 15))
        await _human_delay(0.04, 0.12)


async def _human_fill(scope, selector: str, value: str, timeout=6_000):
    if not value:
        return
    try:
        loc = scope.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        # Click first, then type character by character
        await loc.click()
        await _human_delay(0.1, 0.3)
        for ch in value:
            await loc.type(ch, delay=random.randint(40, 120))
        await _human_delay(0.1, 0.3)
    except Exception:
        pass


async def _try_click_first(page, selectors: list, timeout=5_000) -> bool:
    """Try each selector in order, click the first visible one."""
    for sel in selectors:
        if await _human_click(page, sel, timeout):
            return True
    return False


# ── BookMyShow-specific selectors ─────────────────────────────────────────────

BMS_QTY_DETECT = [
    "text=How many seats",
    "text=How Many Seats",
    "text=how many",
    "[class*='howManySeats']",
    "[class*='qty-picker']",
    "[class*='seatPicker']",
]

BMS_CONTINUE = [
    "button:has-text('Continue')",
    "a:has-text('Continue')",
    "[class*='continue']",
    "[class*='proceed-btn']",
    "button:has-text('Proceed')",
]

BMS_BOOK = [
    "button:has-text('Book')",
    "button:has-text('BOOK')",
    "[class*='book-button']",
    "[class*='bookBtn']",
    "button:has-text('Proceed')",
    "button:has-text('Add to Cart')",
    "a:has-text('Book')",
]

OTP_SCREEN_SELECTORS = [
    "input[placeholder*='OTP' i]",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[autocomplete='one-time-code']",
    "[class*='otp' i] input",
]
OTP_URL_PATTERNS  = ["otp", "verify", "authenticate", "2fa", "confirm"]
SUCCESS_PATTERNS  = ["confirmed", "success", "booking confirmed", "order confirmed", "thank you"]
FAILURE_PATTERNS  = ["failed", "declined", "error", "invalid", "expired", "try again"]


# ── BookMyShow cart flow ──────────────────────────────────────────────────────

async def _bms_handle_popups(page, session_id):
    """Dismiss cookie banners, login prompts, etc."""
    dismiss_selectors = [
        "button:has-text('Accept')",
        "button:has-text('Got It')",
        "button:has-text('OK')",
        "[class*='cookie'] button",
        "[class*='consent'] button",
        "[class*='close-btn']",
        "button[aria-label='Close']",
        "[class*='dialog'] button:has-text('Later')",
        "[class*='modal'] button:has-text('Skip')",
    ]
    for sel in dismiss_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_500):
                await loc.click()
                await _human_delay(0.2, 0.5)
        except Exception:
            pass


async def _bms_select_quantity(page, session_id, max_qty=10):
    """Handle BookMyShow 'How many seats?' popup. Returns True if handled."""
    _update(session_id, message="Selecting seat quantity...")

    # Wait for quantity dialog to appear
    dialog_found = False
    for sel in BMS_QTY_DETECT:
        try:
            if await page.locator(sel).first.is_visible(timeout=8_000):
                dialog_found = True
                break
        except Exception:
            continue

    if not dialog_found:
        logger.info(f"[{session_id}] No quantity dialog — might be direct to map")
        return True

    await _human_delay(0.5, 1.0)

    # Click the highest available number (10, 8, 6, 4, 2, 1)
    selected = False
    for qty in [max_qty, 10, 8, 6, 4, 2, 1]:
        if selected:
            break
        # BookMyShow uses circular number buttons — match exact text
        for tag in ["div", "span", "button", "li", "a"]:
            try:
                num_loc = page.locator(f"{tag}:text-is('{qty}')").first
                if await num_loc.is_visible(timeout=1_200):
                    await _human_click(page, num_loc)
                    selected = True
                    logger.info(f"[{session_id}] Selected quantity: {qty}")
                    break
            except Exception:
                continue

    if not selected:
        # Fallback: try number input
        try:
            inp = page.locator("input[type='number'], input[name*='qty' i]").first
            if await inp.is_visible(timeout=2_000):
                await inp.fill(str(max_qty))
                selected = True
        except Exception:
            pass

    await _human_delay(0.4, 0.8)

    # Click Continue
    for sel in BMS_CONTINUE:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3_000):
                await _human_click(page, btn)
                logger.info(f"[{session_id}] Clicked Continue")
                await _human_delay(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                return True
        except Exception:
            continue

    logger.warning(f"[{session_id}] Could not find Continue button")
    return True


async def _bms_select_category(page, session_id, target_price=""):
    """
    Select seat category on BookMyShow.
    Categories are in a left sidebar showing prices like 499, 1250, 1750, etc.
    Picks target_price if set, otherwise picks the cheapest available.
    """
    _update(session_id, message=f"Selecting seat category{' at Rs.'+target_price if target_price else ' (cheapest)'}...")

    target_num = 0
    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")

    await _human_delay(1.0, 2.0)

    # Collect all price categories visible on the page
    candidates = []

    # Strategy 1: Find elements that look like price category items
    # BookMyShow typically shows categories as list items with price text
    category_container_selectors = [
        "[class*='venueCategory'] [class*='category']",
        "[class*='category-list'] li",
        "[class*='price-card']",
        "[class*='venueSeatLayout'] [class*='category']",
        "[class*='ticketTypes'] li",
        "[class*='type-list'] > div",
        "[class*='side-bar'] [class*='item']",
        "aside li",
    ]

    for container_sel in category_container_selectors:
        try:
            items = await page.locator(container_sel).all()
            for item in items:
                text = (await item.text_content() or "").replace(",", "").replace("₹", "")
                # Extract numeric price
                nums = re.findall(r'\d+', text)
                for num_str in nums:
                    val = int(num_str)
                    if 50 <= val <= 200000:  # reasonable ticket price
                        candidates.append((val, item, text.strip()[:80]))
                        break  # take first price from this element
            if candidates:
                break
        except Exception:
            continue

    # Strategy 2: If no structured categories found, look for bare price text
    if not candidates:
        common_prices = [
            "499", "500", "750", "999", "1000", "1250", "1500", "1750",
            "2000", "2500", "3000", "5000", "7500", "10000", "12000",
            "15000", "17500", "20000", "25000", "40000",
        ]
        for price_str in common_prices:
            try:
                els = await page.locator(f"text=/{price_str}/").all()
                for el in els[:2]:
                    if await el.is_visible(timeout=800):
                        candidates.append((int(price_str), el, price_str))
            except Exception:
                continue

    if not candidates:
        logger.warning(f"[{session_id}] No seat categories found on page")
        return False

    # Remove duplicates by price (keep first found)
    seen_prices = set()
    unique = []
    for val, el, txt in candidates:
        if val not in seen_prices:
            seen_prices.add(val)
            unique.append((val, el, txt))
    candidates = unique

    # Sort by price ascending
    candidates.sort(key=lambda x: x[0])
    logger.info(f"[{session_id}] Found categories: {[f'Rs.{c[0]}' for c in candidates]}")

    # Select: target price or cheapest
    chosen = None
    if target_num:
        # Exact match
        for c in candidates:
            if c[0] == target_num:
                chosen = c
                break
        # Nearest above
        if not chosen:
            for c in candidates:
                if c[0] >= target_num:
                    chosen = c
                    break

    if not chosen:
        chosen = candidates[0]  # cheapest

    val, el, txt = chosen
    logger.info(f"[{session_id}] Selecting category Rs.{val}: {txt}")
    await _human_click(page, el)
    await _human_delay(0.8, 1.5)

    return True


async def _bms_select_subsection(page, session_id):
    """
    After clicking a price category, BookMyShow expands sub-sections
    (e.g., 'KEI Wires-Cables Upper 1 (Rs.499.00)', 'Jio Upper 7 (Rs.499.00)').
    Click the first available sub-section.
    """
    _update(session_id, message="Selecting stand section...")
    await _human_delay(0.5, 1.0)

    subsection_selectors = [
        "[class*='sub-category']",
        "[class*='subCategory']",
        "[class*='venue-block']",
        "[class*='block-name']",
        "[class*='section-name']",
        "[class*='stand']",
    ]

    for sel in subsection_selectors:
        try:
            items = await page.locator(sel).all()
            for item in items:
                text = (await item.text_content() or "").lower()
                # Skip sold-out or unavailable subsections
                if any(x in text for x in ["sold", "unavailable", "no seats"]):
                    continue
                if await item.is_visible(timeout=1_000):
                    await _human_click(page, item)
                    logger.info(f"[{session_id}] Clicked subsection: {text.strip()[:60]}")
                    await _human_delay(0.8, 1.5)
                    return True
        except Exception:
            continue

    # Fallback: click any visible text containing "Upper", "Lower", "Block", "Stand"
    for keyword in ["Upper", "Lower", "Block", "Stand", "Gallery", "Terrace"]:
        try:
            el = page.locator(f"text=/{keyword}/i").first
            if await el.is_visible(timeout=1_500):
                await _human_click(page, el)
                logger.info(f"[{session_id}] Clicked subsection with keyword: {keyword}")
                await _human_delay(0.8, 1.5)
                return True
        except Exception:
            continue

    logger.info(f"[{session_id}] No subsections found — map might be direct")
    return True


async def _bms_select_seats(page, session_id, qty=10):
    """
    Select available seats on the BookMyShow stadium map.
    Available seats are typically colored circles; sold = grey.
    """
    _update(session_id, message=f"Selecting {qty} available seats on map...")
    await _human_delay(1.0, 2.0)

    selected = 0

    # Strategy 1: Seats with explicit availability classes
    seat_selectors = [
        "[class*='seat'][class*='available']:not([class*='sold'])",
        "[class*='seat']:not([class*='sold']):not([class*='blocked']):not([class*='booked']):not([class*='unavailable'])",
        "[data-available='true']",
        "[class*='seatBox']:not([class*='sold'])",
        "[class*='SeatBlock'] [class*='available']",
    ]

    for sel in seat_selectors:
        try:
            seats = await page.locator(sel).all()
            if not seats:
                continue
            logger.info(f"[{session_id}] Found {len(seats)} seats with: {sel}")
            for seat in seats:
                if selected >= qty:
                    break
                try:
                    if await seat.is_visible(timeout=800):
                        await seat.scroll_into_view_if_needed()
                        await _human_click(page, seat)
                        selected += 1
                        await _human_delay(0.08, 0.2)
                except Exception:
                    continue
            if selected > 0:
                break
        except Exception:
            continue

    # Strategy 2: SVG circles on the map (available = colored, sold = grey)
    if selected == 0:
        try:
            circles = await page.locator("svg circle, svg rect, [class*='Seat'] circle").all()
            for circle in circles:
                if selected >= qty:
                    break
                try:
                    fill = (await circle.get_attribute("fill") or "").lower()
                    cls = (await circle.get_attribute("class") or "").lower()
                    style = (await circle.get_attribute("style") or "").lower()

                    grey_markers = ['#ccc', '#ddd', '#eee', 'grey', 'gray', '#999',
                                    'sold', 'blocked', 'booked', 'unavailable', '#e0e0e0']
                    if any(m in (fill + cls + style) for m in grey_markers):
                        continue
                    if not await circle.is_visible(timeout=500):
                        continue

                    await circle.scroll_into_view_if_needed()
                    await _human_click(page, circle)
                    selected += 1
                    await _human_delay(0.08, 0.2)
                except Exception:
                    continue
        except Exception:
            pass

    # Strategy 3: Any clickable seat-like element on the map
    if selected == 0:
        try:
            map_seats = await page.locator("[class*='seat'], [class*='Seat']").all()
            for seat in map_seats:
                if selected >= qty:
                    break
                try:
                    cls = (await seat.get_attribute("class") or "").lower()
                    if any(x in cls for x in ['sold', 'blocked', 'booked', 'unavailable', 'disabled']):
                        continue
                    if await seat.is_visible(timeout=500):
                        await _human_click(page, seat)
                        selected += 1
                        await _human_delay(0.08, 0.2)
                except Exception:
                    continue
        except Exception:
            pass

    logger.info(f"[{session_id}] Selected {selected}/{qty} seats")
    return selected > 0


async def _bms_capture_url(page, session_id):
    """
    Capture the best URL to share with the user.
    Prefers: ticket-options URL > payment URL > current URL.
    """
    url = page.url

    # The ticket-options URL contains venue/show info and is the seat selection page
    if "ticket-options" in url:
        logger.info(f"[{session_id}] Captured ticket-options URL: {url}")
        return url

    if any(k in url.lower() for k in ["cart", "checkout", "payment", "order", "booking"]):
        logger.info(f"[{session_id}] Captured cart/payment URL: {url}")
        return url

    # Try looking for a URL in the page that contains ticket-options
    try:
        links = await page.evaluate("""
            () => {
                const urls = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    if (a.href.includes('ticket-options') || a.href.includes('checkout') || a.href.includes('cart'))
                        urls.push(a.href);
                });
                return urls;
            }
        """)
        if links:
            logger.info(f"[{session_id}] Found link in page: {links[0]}")
            return links[0]
    except Exception:
        pass

    # Fallback: use current URL
    logger.info(f"[{session_id}] Using current URL: {url}")
    return url


# ── Main BookMyShow cart flow ─────────────────────────────────────────────────

async def _run_bms_cart(page, session_id, target_price, watcher_id, max_qty=10):
    """
    Complete BookMyShow flow:
    buytickets → qty → continue → stadium → category → seats → Book → URL
    """
    # Dismiss popups
    await _bms_handle_popups(page, session_id)

    # Step 1: Quantity selection
    await _bms_select_quantity(page, session_id, max_qty)

    # Capture URL here — might already be ticket-options format
    await _human_delay(0.5, 1.0)
    interim_url = page.url
    logger.info(f"[{session_id}] After quantity: {interim_url}")

    # Step 2: Select category (cheapest or target)
    await _bms_select_category(page, session_id, target_price)

    # Step 3: Select sub-section (if expanded)
    await _bms_select_subsection(page, session_id)

    # Step 4: Select individual seats
    await _bms_select_seats(page, session_id, max_qty)

    # Step 5: Capture URL before clicking Book
    pre_book_url = await _bms_capture_url(page, session_id)

    # Step 6: Click Book / Add to Cart
    _update(session_id, message="Adding to cart...")
    await _human_delay(0.5, 1.0)

    clicked_book = await _try_click_first(page, BMS_BOOK, timeout=4_000)
    if clicked_book:
        logger.info(f"[{session_id}] Clicked Book button")
        await _human_delay(2.0, 4.0)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

    # Step 7: Capture final URL
    post_url = await _bms_capture_url(page, session_id)

    # Prefer: ticket-options URL > payment URL > pre-book URL
    cart_url = post_url
    if "ticket-options" in pre_book_url and "ticket-options" not in post_url:
        cart_url = pre_book_url  # the ticket-options URL is more useful

    return cart_url


# ── District.in cart flow ─────────────────────────────────────────────────────

async def _run_district_cart(page, session_id, target_price, watcher_id):
    """District.in: event page → select tier → add to cart → capture URL."""
    _update(session_id, message="Selecting tickets on District...")

    await _human_delay(1.0, 2.0)

    # Try to click a ticket tier / buy button
    tier_selectors = [
        "[class*='ticket-card']:not([class*='sold'])",
        "[class*='tier']:not([class*='unavailable'])",
        "button:has-text('Buy')",
        "button:has-text('Get Tickets')",
        "button:has-text('Book')",
        "a:has-text('Buy Tickets')",
        "[class*='price']:not([class*='sold'])",
    ]

    # If target price, try to find matching tier
    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")
        if target_num:
            try:
                tiers = await page.locator("[class*='ticket'], [class*='tier']").all()
                for tier in tiers:
                    text = (await tier.text_content() or "").replace(",", "")
                    nums = re.findall(r'\d+', text)
                    for n in nums:
                        if int(n) == target_num:
                            await _human_click(page, tier)
                            await _human_delay(1.0, 2.0)
                            break
            except Exception:
                pass

    # Click the first available Buy/Book button
    await _try_click_first(page, tier_selectors, timeout=5_000)
    await _human_delay(1.5, 3.0)

    # Try to proceed / add to cart
    proceed_selectors = [
        "button:has-text('Proceed')",
        "button:has-text('Continue')",
        "button:has-text('Add to Cart')",
        "button:has-text('Checkout')",
        "button[type='submit']",
    ]
    await _try_click_first(page, proceed_selectors, timeout=5_000)
    await _human_delay(1.5, 3.0)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass

    url = page.url
    logger.info(f"[{session_id}] District URL: {url}")
    return url


# ── Main checkout coroutine ──────────────────────────────────────────────────

async def _run_checkout(session_id: str, checkout_url: str, card: dict,
                        cart_mode: bool = True, target_price: str = "",
                        watcher_id: str = ""):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed")
        _update(session_id, status="failed", message="Playwright not installed")
        return

    try:
        from playwright_stealth import Stealth
    except ImportError:
        Stealth = None

    mode_label = "cart" if cart_mode else "checkout"
    _update(session_id, status="running", message=f"Starting {mode_label}...")

    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-web-security",
            ],
        )
        ctx = await browser.new_context(
            user_agent=ua,
            viewport=vp,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )
        await ctx.add_init_script(STEALTH_JS)
        if Stealth is not None:
            try:
                await Stealth(init_scripts_only=True).apply_stealth_async(ctx)
            except Exception as e:
                logger.warning(f"[{session_id}] playwright-stealth failed: {e}")
        page = await ctx.new_page()

        try:
            # ── 1. Navigate ──────────────────────────────────────────────────
            logger.info(f"[{session_id}] Navigating → {checkout_url}")
            _update(session_id, message="Navigating...")
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass

            # Detect platform
            is_bms = "bookmyshow.com" in checkout_url.lower()
            is_district = "district.in" in checkout_url.lower()

            # ── 2. Cart mode ─────────────────────────────────────────────────
            if cart_mode:
                if is_bms:
                    cart_url = await _run_bms_cart(
                        page, session_id, target_price, watcher_id
                    )
                elif is_district:
                    cart_url = await _run_district_cart(
                        page, session_id, target_price, watcher_id
                    )
                else:
                    # Generic fallback
                    cart_url = page.url

                if not _is_actionable_cart_url(cart_url):
                    logger.warning(f"[{session_id}] Could not capture actionable cart URL: {cart_url}")
                    _update(
                        session_id,
                        status="failed",
                        message="Could not capture cart URL after seat selection",
                        cart_url=None,
                    )
                    return

                _update(session_id,
                        status="cart_ready",
                        message="Cart is ready — tap the link to pay",
                        cart_url=cart_url)
                logger.info(f"[{session_id}] Cart URL: {cart_url}")
                if watcher_id:
                    _notify_cart_ready(watcher_id, cart_url)
                return

            # ── 3. Full checkout mode (with card) ────────────────────────────
            profile = _profile()

            # Run the same seat selection flow first
            if is_bms:
                await _run_bms_cart(page, session_id, target_price, watcher_id)
            elif is_district:
                await _run_district_cart(page, session_id, target_price, watcher_id)

            # Fill personal details
            _update(session_id, message="Filling personal details...")
            await _human_fill(page, "input[name*='name' i], input[placeholder*='name' i]", profile["name"])
            await _human_fill(page, "input[type='email'], input[name*='email' i]", profile["email"])
            await _human_fill(page, "input[type='tel'], input[name*='phone' i]", profile["phone"])

            # Fill card details
            _update(session_id, message=f"Filling card #{card['priority']}...")
            await _human_fill(
                page,
                "input[name*='card'][name*='number' i], input[placeholder*='card number' i]",
                card["number"],
            )
            await _human_fill(
                page,
                "input[name*='expiry' i], input[placeholder*='MM/YY' i]",
                card["expiry"],
            )
            await _human_fill(
                page,
                "input[name*='cvv' i], input[placeholder*='CVV' i]",
                card["cvv"],
            )

            # Try payment iframes
            for iframe_sel in ["iframe[src*='razorpay']", "iframe[src*='stripe']",
                               "iframe[name*='card']", "iframe[title*='payment' i]"]:
                try:
                    await page.wait_for_selector(iframe_sel, timeout=3_000)
                    f = page.frame_locator(iframe_sel)
                    await _human_fill(f, "input[name*='number' i], input[placeholder*='Card number' i]", card["number"])
                    await _human_fill(f, "input[name*='expiry' i], input[placeholder*='MM' i]", card["expiry"])
                    await _human_fill(f, "input[name*='cvv' i], input[placeholder*='CVV' i]", card["cvv"])
                    break
                except Exception:
                    pass

            # Click Pay
            await _try_click_first(page, [
                "button:has-text('Pay Now')",
                "button:has-text('Confirm')",
                "button:has-text('Place Order')",
                "button[class*='pay' i]",
                "button[class*='confirm' i]",
                "button[type='submit']",
            ])
            await _human_delay(1.5, 3.0)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass

            # ── 4. OTP gate ──────────────────────────────────────────────────
            is_otp = False
            if any(p in page.url.lower() for p in OTP_URL_PATTERNS):
                is_otp = True
            else:
                for sel in OTP_SCREEN_SELECTORS:
                    try:
                        if await page.locator(sel).first.is_visible(timeout=1_500):
                            is_otp = True
                            break
                    except Exception:
                        pass

            if is_otp:
                logger.info(f"[{session_id}] OTP screen detected")
                _update(session_id,
                        status="otp_required",
                        message=f"Card #{card['priority']} — enter OTP to confirm")

                otp = await _wait_for_otp(session_id, timeout_s=300)
                if not otp:
                    raise TimeoutError("OTP not received within 5 minutes")

                _update(session_id, message="Submitting OTP...")
                for sel in OTP_SCREEN_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=2_000):
                            await loc.fill(otp)
                            await _human_delay(0.3, 0.7)
                            break
                    except Exception:
                        pass

                await _try_click_first(page, [
                    "button:has-text('Submit')",
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button[type='submit']",
                ])
                await _human_delay(2.0, 4.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

            # ── 5. Outcome ───────────────────────────────────────────────────
            body = (await page.text_content("body") or "").lower()
            url = page.url.lower()

            if any(p in url or p in body for p in SUCCESS_PATTERNS):
                _update(session_id, status="success",
                        message=f"Card #{card['priority']} — Booking confirmed!")
                logger.info(f"[{session_id}] CONFIRMED")
            else:
                reason = next((p for p in FAILURE_PATTERNS if p in body), "unknown")
                _update(session_id, status="failed",
                        message=f"Card #{card['priority']} failed ({reason})")
                logger.warning(f"[{session_id}] failed — {reason}")

        except Exception as e:
            logger.error(f"[{session_id}] error: {e}")
            _update(session_id, status="failed", message=str(e))
        finally:
            await ctx.close()
            await browser.close()


async def _wait_for_otp(session_id: str, timeout_s=300) -> Optional[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _sessions_lock:
            otp = _sessions.get(session_id, {}).get("otp")
        if otp:
            return otp
        await asyncio.sleep(2)
    return None


def _notify_cart_ready(watcher_id: str, cart_url: str):
    """Posts cart URL back to Flask so it can push-notify the user."""
    port = os.environ.get("PORT", "8000")
    try:
        import requests as req
        req.post(
            f"http://127.0.0.1:{port}/api/watchers/{watcher_id}/cart-url",
            json={"cart_url": cart_url},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Could not notify cart URL: {e}")


# ── Thread entry point ────────────────────────────────────────────────────────

def _run_in_thread(session_id: str, checkout_url: str, card: dict,
                   cart_mode: bool, target_price: str, watcher_id: str):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            _run_checkout(session_id, checkout_url, card,
                          cart_mode=cart_mode,
                          target_price=target_price,
                          watcher_id=watcher_id)
        )
    finally:
        loop.close()


def _derive_buytickets_url(event_url: str) -> str:
    """
    Convert any BMS event URL to the buytickets entry point.
    /sports/slug/ETXXXXXX  →  /buytickets/slug/ETXXXXXX
    """
    import re
    m = re.search(r'in\.bookmyshow\.com/(?:sports|events)/([^?#]+)', event_url)
    if m:
        slug = m.group(1).rstrip('/')
        return f"https://in.bookmyshow.com/buytickets/{slug}"
    if 'buytickets' in event_url:
        return event_url
    return event_url


def _cleanup_stale_sessions(max_age_s=1800):
    """Remove sessions older than max_age_s (default 30 min) to prevent memory leaks."""
    now = time.time()
    with _sessions_lock:
        stale = [sid for sid, s in _sessions.items()
                 if now - s.get("created_at", now) > max_age_s
                 and s.get("status") not in ("running", "otp_required")]
        for sid in stale:
            del _sessions[sid]
    if stale:
        logger.info(f"Cleaned up {len(stale)} stale sessions")


def trigger_auto_checkout(watcher_id: str, checkout_url: str,
                          cart_mode: bool = True,
                          target_price: str = "",
                          owner_email: str = ""):
    """
    cart_mode=True  → instantly derive buytickets URL and send to user.
                      BookMyShow blocks headless browsers, so we skip Playwright
                      and send the direct booking link for the user to open.
    cart_mode=False → full checkout including card fill and OTP (needs cards).
    """
    _cleanup_stale_sessions()
    pool = _load_card_pool()

    if cart_mode:
        sid = _session_id(watcher_id, 1)
        launch_url = _derive_buytickets_url(checkout_url)

        with _sessions_lock:
            existing = _sessions.get(sid, {}).get("status")
            if existing in ("running", "otp_required", "cart_ready"):
                logger.info(f"[{sid}] Already running — skipping")
                return
            # Set session atomically (no gap between check and set)
            _sessions[sid] = {
                "status":        "running",
                "message":       "Starting cart automation...",
                "otp":           None,
                "device_id":     None,
                "card_priority": 1,
                "cart_url":      None,
                "cart_mode":     True,
                "created_at":    time.time(),
            }

        logger.info(f"[{sid}] CART MODE — launching browser flow: {launch_url}")
        placeholder_card = {"priority": 1, "number": "", "expiry": "", "cvv": "", "name": ""}
        _enqueue_checkout(sid, launch_url, placeholder_card, True, target_price, watcher_id)
        return

    # Full checkout mode
    if not pool:
        logger.warning("No cards configured — set CARD_1_NUMBER etc. in env vars")
        return

    for card in pool:
        sid = _session_id(watcher_id, card["priority"])

        with _sessions_lock:
            existing = _sessions.get(sid, {}).get("status")
            if existing in ("running", "otp_required", "cart_ready"):
                logger.info(f"[{sid}] Already running — skipping")
                continue
            _sessions[sid] = {
                "status":        "running",
                "message":       "Starting...",
                "otp":           None,
                "device_id":     None,
                "card_priority": card["priority"],
                "cart_url":      None,
                "cart_mode":     False,
                "created_at":    time.time(),
            }

        logger.info(f"[{sid}] Launching FULL CHECKOUT with card #{card['priority']}"
                    + (f" targeting Rs.{target_price}" if target_price else ""))
        _enqueue_checkout(sid, checkout_url, card, False, target_price, watcher_id)
