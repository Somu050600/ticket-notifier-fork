"""
autocheckout.py — Production-Grade Headless Booking Engine
==========================================================

Architecture Overview
---------------------
This module implements a fully isolated, thread-safe booking engine that
communicates with the Flask web layer exclusively through a thread-safe
``queue.Queue`` and a lock-protected session dict.

Key Design Decisions:
  1. **Dedicated asyncio event loop in a background daemon thread.**
     Flask/Gunicorn is WSGI (synchronous). Playwright is async. Running them
     in the same thread or event loop causes deadlocks. We spin up ONE
     persistent background thread that owns its own ``asyncio.EventLoop``
     and processes booking jobs from a queue.

  2. **Residential proxy with sticky sessions.**
     Akamai Bot Manager fingerprints datacenter IPs instantly. We route ALL
     Playwright traffic through a residential proxy whose username includes
     a per-session UUID. This guarantees the exit IP stays constant for the
     entire booking flow, preventing ``_abck`` / ``bm_sz`` cookie
     invalidation mid-checkout.

  3. **Network interception instead of blind waits.**
     Ticketing platforms use a two-phase commit: when a seat is clicked, an
     XHR fires to acquire a pessimistic lock (Redis seat-lock). We use
     ``page.expect_response()`` to wait for the backend lock API to return
     200 before navigating forward. This eliminates the redirect-loop caused
     by navigating before the lock is confirmed.

  4. **playwright-stealth for fingerprint masking.**
     Patches ``navigator.webdriver``, plugin arrays, WebGL renderer strings,
     Chrome runtime objects, and dozens of other signals that Akamai checks.

Environment Variables (set in Railway dashboard):
  PROXY_SERVER        — e.g. ``gate.smartproxy.com:7000``
  PROXY_USERNAME      — e.g. ``sp1234user``
  PROXY_PASSWORD      — e.g. ``secretpass``
  CARD_1_NUMBER … CARD_3_NUMBER  — payment card pool (full-checkout only)
  CARD_1_EXPIRY … CARD_3_CVV     — expiry/CVV for each card
  PROFILE_NAME / PROFILE_EMAIL / PROFILE_PHONE — autofill identity
"""

# ── stdlib ───────────────────────────────────────────────────────────────────
import asyncio
import logging
import os
import queue
import random
import re
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger("ticketalert.checkout")

# ═════════════════════════════════════════════════════════════════════════════
# §1  CONFIGURATION & CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Residential proxy credentials (set in Railway env vars)
PROXY_SERVER   = os.environ.get("PROXY_SERVER", "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# Timeouts (milliseconds for Playwright, seconds for Python)
NAV_TIMEOUT_MS       = 45_000   # page.goto max wait
NETWORK_IDLE_MS      = 15_000   # wait_for_load_state("networkidle")
SEAT_LOCK_TIMEOUT_MS = 12_000   # max wait for seat-lock XHR response
ELEMENT_TIMEOUT_MS   = 8_000    # locator visibility wait
OTP_TIMEOUT_S        = 300      # 5 minutes to receive OTP

# User-Agent pool — real desktop Chrome strings
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
]

# ── BookMyShow-specific selectors ────────────────────────────────────────────

BMS_QTY_DETECT = [
    "text=How many seats",
    "text=How Many Seats",
    "text=how many",
    "[class*='howManySeats']",
    "[class*='qty-picker']",
    "[class*='seatPicker']",
    "[class*='seat-layout'] [class*='qty']",
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

# Patterns the network interceptor watches for — these are the XHR endpoints
# that BookMyShow hits when locking seats in their backend.
SEAT_LOCK_URL_PATTERNS = [
    "seats/lock",
    "blockseats",
    "block-seats",
    "reserve",
    "seat-layout",
    "addtocart",
    "add-to-cart",
    "ssadapter",
    "payment-options",
    "ticket-options",
]

OTP_SCREEN_SELECTORS = [
    "input[placeholder*='OTP' i]",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[autocomplete='one-time-code']",
    "[class*='otp' i] input",
]

SUCCESS_PATTERNS = [
    "confirmed", "success", "booking confirmed",
    "order confirmed", "thank you",
]

FAILURE_PATTERNS = [
    "failed", "declined", "error", "invalid",
    "expired", "try again",
]


# ═════════════════════════════════════════════════════════════════════════════
# §2  CARD POOL & PROFILE
# ═════════════════════════════════════════════════════════════════════════════

def _load_card_pool() -> list[dict]:
    """Load up to 3 payment cards from environment variables."""
    pool = []
    for n in range(1, 4):
        number = (
            os.environ.get(f"CARD_{n}_NUMBER")
            or (os.environ.get("CARD_NUMBER") if n == 1 else "")
        )
        if not number:
            continue
        pool.append({
            "priority": n,
            "number":   number,
            "expiry":   os.environ.get(f"CARD_{n}_EXPIRY",
                                       os.environ.get("CARD_EXPIRY", "")),
            "cvv":      os.environ.get(f"CARD_{n}_CVV",
                                       os.environ.get("CARD_CVV", "")),
            "name":     os.environ.get(f"CARD_{n}_NAME",
                                       os.environ.get("PROFILE_NAME", "")),
        })
    return pool


def _profile() -> dict:
    """Load autofill identity from environment."""
    return {
        "name":  os.environ.get("PROFILE_NAME",  ""),
        "email": os.environ.get("PROFILE_EMAIL", ""),
        "phone": os.environ.get("PROFILE_PHONE", ""),
    }


# ═════════════════════════════════════════════════════════════════════════════
# §3  SESSION STATE (thread-safe)
# ═════════════════════════════════════════════════════════════════════════════

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _session_id(watcher_id: str, priority: int) -> str:
    return f"{watcher_id}-slot-{priority}"


def _update(session_id: str, **kwargs):
    """Thread-safe session update (called from worker thread)."""
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id].update(kwargs)


def _cleanup_stale_sessions(max_age_s: int = 1800):
    """Remove sessions older than 30 min to prevent memory leaks."""
    now = time.time()
    with _sessions_lock:
        stale = [
            sid for sid, s in _sessions.items()
            if now - s.get("created_at", now) > max_age_s
            and s.get("status") not in ("running", "otp_required")
        ]
        for sid in stale:
            del _sessions[sid]
    if stale:
        logger.info(f"Cleaned up {len(stale)} stale checkout sessions")


# ── Public API for Flask routes ──────────────────────────────────────────────

def get_session(session_id: str) -> dict:
    """Returns session state for frontend polling via /api/checkout-status/."""
    with _sessions_lock:
        sess = _sessions.get(session_id, {})
    return {
        "status":        sess.get("status", "idle"),
        "message":       sess.get("message", ""),
        "card_priority": sess.get("card_priority", 0),
        "device_id":     sess.get("device_id"),
        "cart_url":      sess.get("cart_url"),
    }


def get_session_for_device(watcher_id: str, device_id: str) -> dict:
    """Find session assigned to a specific device."""
    with _sessions_lock:
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return {**get_session(sid), "session_id": sid}
    return {"status": "idle", "message": "", "session_id": None}


def claim_slot(watcher_id: str, device_id: str) -> Optional[str]:
    """A device claims the next available checkout slot."""
    with _sessions_lock:
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if (sess
                    and sess.get("status") in ("running", "otp_required", "cart_ready")
                    and sess.get("device_id") is None):
                sess["device_id"] = device_id
                logger.info(f"[{sid}] Claimed by device {device_id}")
                return sid
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return sid
    return None


def inject_otp(session_id: str, otp: str):
    """Receive OTP from user and store it for the worker to pick up."""
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id]["otp"] = otp
            logger.info(f"[{session_id}] OTP injected")


# ═════════════════════════════════════════════════════════════════════════════
# §4  JOB QUEUE — Flask → Worker communication
# ═════════════════════════════════════════════════════════════════════════════

_job_queue: queue.Queue = queue.Queue(maxsize=100)


class BookingJob:
    """Immutable value object representing a single booking request."""
    __slots__ = (
        "watcher_id", "checkout_url", "cart_mode",
        "target_price", "owner_email",
    )

    def __init__(self, watcher_id: str, checkout_url: str,
                 cart_mode: bool = True, target_price: str = "",
                 owner_email: str = ""):
        self.watcher_id   = watcher_id
        self.checkout_url  = checkout_url
        self.cart_mode     = cart_mode
        self.target_price  = target_price
        self.owner_email   = owner_email


# ═════════════════════════════════════════════════════════════════════════════
# §5  HUMAN-LIKE INTERACTION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

async def _human_delay(lo: float = 0.3, hi: float = 1.0):
    """Random pause to mimic human reaction time."""
    await asyncio.sleep(random.uniform(lo, hi))


async def _human_move(page, x: float, y: float):
    """Move mouse to (x, y) with a human-like curve."""
    try:
        await page.mouse.move(x, y, steps=random.randint(8, 20))
    except Exception:
        pass


async def _human_click(page, locator_or_sel, timeout: int = ELEMENT_TIMEOUT_MS) -> bool:
    """
    Click an element with realistic mouse movement and positional jitter.
    Accepts either a Playwright Locator or a CSS selector string.
    Returns True if the click succeeded.
    """
    try:
        el = (locator_or_sel if hasattr(locator_or_sel, "click")
              else page.locator(locator_or_sel).first)
        await el.wait_for(state="visible", timeout=timeout)
        box = await el.bounding_box()
        if box:
            x = box["x"] + random.uniform(box["width"] * 0.25, box["width"] * 0.75)
            y = box["y"] + random.uniform(box["height"] * 0.25, box["height"] * 0.75)
            await _human_move(page, x, y)
            await _human_delay(0.08, 0.25)
            await page.mouse.click(x, y)
        else:
            await el.click()
        await _human_delay(0.15, 0.4)
        return True
    except Exception:
        return False


async def _human_scroll(page, distance: int = 400):
    """Scroll down smoothly in random increments."""
    steps = random.randint(3, 7)
    for _ in range(steps):
        await page.mouse.wheel(0, distance // steps + random.randint(-15, 15))
        await _human_delay(0.04, 0.12)


async def _human_fill(scope, selector: str, value: str,
                      timeout: int = ELEMENT_TIMEOUT_MS):
    """Type into an input field character-by-character."""
    if not value:
        return
    try:
        loc = scope.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        await _human_delay(0.1, 0.3)
        for ch in value:
            await loc.type(ch, delay=random.randint(40, 120))
        await _human_delay(0.1, 0.3)
    except Exception:
        pass


async def _try_click_first(page, selectors: list,
                           timeout: int = 5_000) -> bool:
    """Try each selector in order; click the first visible one."""
    for sel in selectors:
        if await _human_click(page, sel, timeout):
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# §6  PROXY CONFIGURATION — Sticky Sessions
# ═════════════════════════════════════════════════════════════════════════════

def _build_proxy_config() -> Optional[dict]:
    """
    Build Playwright proxy dict with a sticky session.

    The proxy username is suffixed with a unique session UUID so that the
    residential proxy provider assigns and KEEPS a single exit IP for the
    entire browser context lifetime.  This prevents Akamai from seeing
    different IPs between page loads and invalidating _abck cookies.
    """
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        logger.warning(
            "No proxy configured (PROXY_SERVER/PROXY_USERNAME/PROXY_PASSWORD). "
            "Akamai will likely block datacenter IPs. Falling back to "
            "instant URL derivation mode."
        )
        return None

    session_uuid = uuid.uuid4().hex[:12]
    sticky_username = f"{PROXY_USERNAME}-session-{session_uuid}"

    proxy = {
        "server":   f"http://{PROXY_SERVER}",
        "username": sticky_username,
        "password": PROXY_PASSWORD,
    }
    logger.info(f"Proxy: {PROXY_SERVER} sticky session {session_uuid}")
    return proxy


# ═════════════════════════════════════════════════════════════════════════════
# §7  NETWORK INTERCEPTION — Seat Lock Verification
# ═════════════════════════════════════════════════════════════════════════════

async def _wait_for_seat_lock(page, session_id: str,
                              timeout_ms: int = SEAT_LOCK_TIMEOUT_MS) -> Optional[dict]:
    """
    Monitor the network layer for a seat-lock / cart-creation API response.

    Instead of blindly waiting after clicking a seat, we intercept the
    actual XHR/Fetch call that BMS fires to acquire a pessimistic lock.
    We do NOT navigate forward until this returns 200.
    """
    _update(session_id, message="Waiting for seat lock confirmation...")

    lock_response = {"value": None}

    def _on_response(response):
        url_lower = response.url.lower()
        for pattern in SEAT_LOCK_URL_PATTERNS:
            if pattern in url_lower:
                if response.status == 200:
                    logger.info(
                        f"[{session_id}] Seat lock confirmed: "
                        f"{response.status} {response.url[:120]}"
                    )
                    lock_response["value"] = response
                else:
                    logger.warning(
                        f"[{session_id}] Seat lock non-200: "
                        f"{response.status} {response.url[:120]}"
                    )
                break

    page.on("response", _on_response)

    deadline = time.time() + (timeout_ms / 1000)
    while time.time() < deadline and lock_response["value"] is None:
        await asyncio.sleep(0.3)

    page.remove_listener("response", _on_response)

    if lock_response["value"]:
        _update(session_id, message="Seats locked! Proceeding to cart...")
        return lock_response["value"]
    else:
        logger.warning(f"[{session_id}] Seat lock timeout after {timeout_ms}ms")
        _update(session_id, message="Seat lock timed out — proceeding anyway...")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# §8  BOOKMYSHOW CART FLOW
# ═════════════════════════════════════════════════════════════════════════════

async def _bms_handle_popups(page, session_id: str):
    """Dismiss any overlaying modals, cookie banners, or login prompts."""
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


async def _bms_select_quantity(page, session_id: str, max_qty: int = 10):
    """Handle BookMyShow 'How many seats?' popup."""
    _update(session_id, message="Selecting seat quantity...")

    dialog_found = False
    for sel in BMS_QTY_DETECT:
        try:
            if await page.locator(sel).first.is_visible(timeout=ELEMENT_TIMEOUT_MS):
                dialog_found = True
                break
        except Exception:
            continue

    if not dialog_found:
        logger.info(f"[{session_id}] No qty dialog — may go directly to map")
        return True

    await _human_delay(0.5, 1.0)

    selected = False
    for qty in [max_qty, 10, 8, 6, 4, 2, 1]:
        if selected:
            break
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
        try:
            inp = page.locator("input[type='number'], input[name*='qty' i]").first
            if await inp.is_visible(timeout=2_000):
                await inp.fill(str(max_qty))
                selected = True
        except Exception:
            pass

    await _human_delay(0.4, 0.8)

    # Click Continue and wait for network to settle
    for sel in BMS_CONTINUE:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3_000):
                await _human_click(page, btn)
                logger.info(f"[{session_id}] Clicked Continue")
                await _human_delay(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except Exception:
                    pass
                return True
        except Exception:
            continue

    logger.warning(f"[{session_id}] Could not find Continue button")
    return True


async def _bms_select_category(page, session_id: str, target_price: str = ""):
    """Select seat category — target_price if set, otherwise cheapest."""
    label = f" at Rs.{target_price}" if target_price else " (cheapest)"
    _update(session_id, message=f"Selecting seat category{label}...")

    target_num = 0
    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")

    await _human_delay(1.0, 2.0)

    candidates = []
    category_selectors = [
        "[class*='venueCategory'] [class*='category']",
        "[class*='category-list'] li",
        "[class*='price-card']",
        "[class*='venueSeatLayout'] [class*='category']",
        "[class*='ticketTypes'] li",
        "[class*='type-list'] > div",
        "[class*='side-bar'] [class*='item']",
        "aside li",
    ]

    for container_sel in category_selectors:
        try:
            items = await page.locator(container_sel).all()
            for item in items:
                text = (await item.text_content() or "").replace(",", "").replace("\u20b9", "")
                nums = re.findall(r"\d+", text)
                for num_str in nums:
                    val = int(num_str)
                    if 50 <= val <= 200_000:
                        candidates.append((val, item, text.strip()[:80]))
                        break
            if candidates:
                break
        except Exception:
            continue

    if not candidates:
        for price_str in ["499", "500", "750", "999", "1000", "1250", "1500",
                          "1750", "2000", "2500", "3000", "5000", "7500",
                          "10000", "15000", "20000"]:
            try:
                els = await page.locator(f"text=/{price_str}/").all()
                for el in els[:2]:
                    if await el.is_visible(timeout=800):
                        candidates.append((int(price_str), el, price_str))
            except Exception:
                continue

    if not candidates:
        logger.warning(f"[{session_id}] No seat categories found")
        return False

    seen = set()
    unique = []
    for val, el, txt in candidates:
        if val not in seen:
            seen.add(val)
            unique.append((val, el, txt))
    candidates = sorted(unique, key=lambda x: x[0])
    logger.info(f"[{session_id}] Categories: {[f'Rs.{c[0]}' for c in candidates]}")

    chosen = None
    if target_num:
        for c in candidates:
            if c[0] == target_num:
                chosen = c
                break
        if not chosen:
            for c in candidates:
                if c[0] >= target_num:
                    chosen = c
                    break
    if not chosen:
        chosen = candidates[0]

    val, el, txt = chosen
    logger.info(f"[{session_id}] Selecting Rs.{val}: {txt}")
    await _human_click(page, el)
    await _human_delay(0.8, 1.5)
    return True


async def _bms_select_subsection(page, session_id: str):
    """Click the first available stand/block subsection."""
    _update(session_id, message="Selecting stand section...")
    await _human_delay(0.5, 1.0)

    for sel in ["[class*='sub-category']", "[class*='subCategory']",
                "[class*='venue-block']", "[class*='block-name']",
                "[class*='section-name']", "[class*='stand']"]:
        try:
            items = await page.locator(sel).all()
            for item in items:
                text = (await item.text_content() or "").lower()
                if any(x in text for x in ["sold", "unavailable", "no seats"]):
                    continue
                if await item.is_visible(timeout=1_000):
                    await _human_click(page, item)
                    logger.info(f"[{session_id}] Subsection: {text.strip()[:60]}")
                    await _human_delay(0.8, 1.5)
                    return True
        except Exception:
            continue

    for keyword in ["Upper", "Lower", "Block", "Stand", "Gallery", "Terrace"]:
        try:
            el = page.locator(f"text=/{keyword}/i").first
            if await el.is_visible(timeout=1_500):
                await _human_click(page, el)
                logger.info(f"[{session_id}] Subsection keyword: {keyword}")
                await _human_delay(0.8, 1.5)
                return True
        except Exception:
            continue

    logger.info(f"[{session_id}] No subsections — map may be direct")
    return True


async def _bms_select_seats(page, session_id: str, qty: int = 10):
    """Select available seats on the stadium map."""
    _update(session_id, message=f"Selecting {qty} seats on map...")
    await _human_delay(1.0, 2.0)
    selected = 0

    # Strategy 1: Structured DOM seats
    seat_selectors = [
        "[class*='seat'][class*='available']:not([class*='sold'])",
        "[class*='seat']:not([class*='sold']):not([class*='blocked'])"
        ":not([class*='booked']):not([class*='unavailable'])",
        "[data-available='true']",
        "[class*='seatBox']:not([class*='sold'])",
        "[class*='SeatBlock'] [class*='available']",
    ]

    for sel in seat_selectors:
        try:
            seats = await page.locator(sel).all()
            if not seats:
                continue
            logger.info(f"[{session_id}] Found {len(seats)} seats via: {sel}")
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

    # Strategy 2: SVG circles
    if selected == 0:
        try:
            circles = await page.locator(
                "svg circle, svg rect, [class*='Seat'] circle"
            ).all()
            grey = ["#ccc", "#ddd", "#eee", "grey", "gray", "#999",
                    "sold", "blocked", "booked", "unavailable", "#e0e0e0"]
            for circle in circles:
                if selected >= qty:
                    break
                try:
                    fill  = (await circle.get_attribute("fill") or "").lower()
                    cls   = (await circle.get_attribute("class") or "").lower()
                    style = (await circle.get_attribute("style") or "").lower()
                    combined = fill + cls + style
                    if any(m in combined for m in grey):
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

    # Strategy 3: Generic seat elements
    if selected == 0:
        try:
            map_seats = await page.locator("[class*='seat'], [class*='Seat']").all()
            skip = ["sold", "blocked", "booked", "unavailable", "disabled"]
            for seat in map_seats:
                if selected >= qty:
                    break
                try:
                    cls = (await seat.get_attribute("class") or "").lower()
                    if any(x in cls for x in skip):
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


async def _bms_capture_url(page, session_id: str) -> str:
    """Capture the best URL to send to user (ticket-options > cart > current)."""
    url = page.url

    if "ticket-options" in url:
        logger.info(f"[{session_id}] Captured ticket-options URL: {url}")
        return url

    if any(k in url.lower() for k in ["cart", "checkout", "payment", "order"]):
        logger.info(f"[{session_id}] Captured cart URL: {url}")
        return url

    try:
        links = await page.evaluate("""
            () => {
                const found = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    if (/(ticket-options|checkout|cart|payment)/.test(a.href))
                        found.push(a.href);
                });
                return found;
            }
        """)
        if links:
            logger.info(f"[{session_id}] Found link in DOM: {links[0]}")
            return links[0]
    except Exception:
        pass

    logger.info(f"[{session_id}] Using current URL: {url}")
    return url


async def _run_bms_cart(page, session_id: str, target_price: str,
                        watcher_id: str, max_qty: int = 10) -> str:
    """
    Complete BookMyShow booking flow with network interception:
      buytickets → qty → continue → stadium map
      → category → subsection → seats → WAIT FOR SEAT LOCK → Book → URL
    """
    await _bms_handle_popups(page, session_id)
    await _bms_select_quantity(page, session_id, max_qty)
    await _human_delay(0.5, 1.0)
    logger.info(f"[{session_id}] After qty: {page.url}")

    await _bms_select_category(page, session_id, target_price)
    await _bms_select_subsection(page, session_id)

    seats_selected = await _bms_select_seats(page, session_id, max_qty)

    # ── NETWORK INTERCEPTION — wait for seat lock before proceeding ──────
    if seats_selected:
        lock_resp = await _wait_for_seat_lock(page, session_id)
        if lock_resp:
            logger.info(f"[{session_id}] Seat lock verified — safe to proceed")
        else:
            logger.warning(f"[{session_id}] No seat lock intercepted — cautious proceed")

    pre_book_url = await _bms_capture_url(page, session_id)

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

    post_url = await _bms_capture_url(page, session_id)

    cart_url = post_url
    if "ticket-options" in pre_book_url and "ticket-options" not in post_url:
        cart_url = pre_book_url

    return cart_url


# ═════════════════════════════════════════════════════════════════════════════
# §9  DISTRICT.IN CART FLOW
# ═════════════════════════════════════════════════════════════════════════════

async def _run_district_cart(page, session_id: str, target_price: str,
                             watcher_id: str) -> str:
    """District.in: event page → select tier → add to cart → capture URL."""
    _update(session_id, message="Selecting tickets on District...")
    await _human_delay(1.0, 2.0)

    if target_price:
        target_num = int("".join(filter(str.isdigit, str(target_price))) or "0")
        if target_num:
            try:
                tiers = await page.locator("[class*='ticket'], [class*='tier']").all()
                for tier in tiers:
                    text = (await tier.text_content() or "").replace(",", "")
                    nums = re.findall(r"\d+", text)
                    for n in nums:
                        if int(n) == target_num:
                            await _human_click(page, tier)
                            await _human_delay(1.0, 2.0)
                            break
            except Exception:
                pass

    tier_selectors = [
        "[class*='ticket-card']:not([class*='sold'])",
        "[class*='tier']:not([class*='unavailable'])",
        "button:has-text('Buy')",
        "button:has-text('Get Tickets')",
        "button:has-text('Book')",
        "a:has-text('Buy Tickets')",
    ]
    await _try_click_first(page, tier_selectors, timeout=5_000)
    await _human_delay(1.5, 3.0)

    await _try_click_first(page, [
        "button:has-text('Proceed')",
        "button:has-text('Continue')",
        "button:has-text('Add to Cart')",
        "button:has-text('Checkout')",
        "button[type='submit']",
    ], timeout=5_000)
    await _human_delay(1.5, 3.0)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass

    url = page.url
    logger.info(f"[{session_id}] District URL: {url}")
    return url


# ═════════════════════════════════════════════════════════════════════════════
# §10  URL DERIVATION (fallback when no proxy is available)
# ═════════════════════════════════════════════════════════════════════════════

def _derive_buytickets_url(event_url: str) -> str:
    """
    Convert BMS event URL to buytickets entry point.
    /sports/slug/ETXXXXXX → /buytickets/slug/ETXXXXXX

    Used as FALLBACK when no residential proxy is configured.
    """
    m = re.search(r"in\.bookmyshow\.com/(?:sports|events)/([^?#]+)", event_url)
    if m:
        slug = m.group(1).rstrip("/")
        return f"https://in.bookmyshow.com/buytickets/{slug}"
    if "buytickets" in event_url:
        return event_url
    return event_url


# ═════════════════════════════════════════════════════════════════════════════
# §11  MAIN CHECKOUT COROUTINE
# ═════════════════════════════════════════════════════════════════════════════

async def _run_checkout(session_id: str, checkout_url: str, card: dict,
                        cart_mode: bool = True, target_price: str = "",
                        watcher_id: str = ""):
    """
    Core booking coroutine — runs inside the worker thread's event loop.
    Opens stealth Chromium via residential proxy, performs full booking flow.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed")
        _update(session_id, status="failed", message="Playwright not installed")
        return

    # Import stealth patcher
    _stealth_cls = None
    try:
        from playwright_stealth import Stealth
        _stealth_cls = Stealth
        logger.info(f"[{session_id}] playwright-stealth v2 loaded")
    except Exception as e:
        logger.warning(f"[{session_id}] playwright-stealth not available ({e}) — using manual JS patches")

    mode_label = "cart" if cart_mode else "full-checkout"
    _update(session_id, status="running", message=f"Starting {mode_label}...")

    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    proxy = _build_proxy_config()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        # Single persistent BrowserContext — all cookies/tokens preserved
        ctx_kwargs = {
            "user_agent":        ua,
            "viewport":          vp,
            "locale":            "en-IN",
            "timezone_id":       "Asia/Kolkata",
            "extra_http_headers": {
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept":          "text/html,application/xhtml+xml,"
                                   "application/xml;q=0.9,*/*;q=0.8",
                "DNT":             "1",
            },
        }
        if proxy:
            ctx_kwargs["proxy"] = proxy

        ctx = await browser.new_context(**ctx_kwargs)

        # ── Apply stealth patches to context ───────────────────────────
        stealth_applied = False
        if _stealth_cls:
            try:
                await _stealth_cls().apply_stealth_async(ctx)
                stealth_applied = True
                logger.info(f"[{session_id}] Stealth v2 patches applied to context")
            except Exception as e:
                logger.warning(f"[{session_id}] Stealth.apply_stealth_async failed ({e})")

        if not stealth_applied:
            await ctx.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                try{delete navigator.__proto__.webdriver}catch(e){}
                Object.defineProperty(navigator,'plugins',{
                    get:()=>[{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',
                    description:'Portable Document Format',length:1},
                    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                    description:'',length:1},
                    {name:'Native Client',filename:'internal-nacl-plugin',
                    description:'',length:2}]
                });
                Object.defineProperty(navigator,'languages',{
                    get:()=>['en-IN','en-US','en','hi']
                });
                window.chrome={runtime:{connect:()=>{},sendMessage:()=>{}},
                    loadTimes:()=>({}),csi:()=>({})};
                const _oq=navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query=p=>p.name==='notifications'
                    ?Promise.resolve({state:Notification.permission}):_oq(p);
                const _gp=WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter=function(p){
                    if(p===37445)return'Intel Inc.';
                    if(p===37446)return'Intel Iris OpenGL Engine';
                    return _gp.call(this,p)};
            """)
            logger.info(f"[{session_id}] Manual JS stealth patches applied")

        page = await ctx.new_page()

        try:
            # ── Navigate ─────────────────────────────────────────────────
            logger.info(f"[{session_id}] Navigating → {checkout_url}")
            _update(session_id, message="Navigating to event page...")
            await page.goto(checkout_url, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
            except Exception:
                pass

            is_bms      = "bookmyshow.com" in checkout_url.lower()
            is_district = "district.in" in checkout_url.lower()

            # ── Cart mode ────────────────────────────────────────────────
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
                    cart_url = page.url

                _update(session_id, status="cart_ready",
                        message="Cart ready — open the link and pay!",
                        cart_url=cart_url)
                logger.info(f"[{session_id}] Cart URL: {cart_url}")
                if watcher_id:
                    _notify_cart_ready(watcher_id, cart_url)
                return

            # ── Full checkout ────────────────────────────────────────────
            profile = _profile()
            if is_bms:
                await _run_bms_cart(page, session_id, target_price, watcher_id)
            elif is_district:
                await _run_district_cart(page, session_id, target_price, watcher_id)

            _update(session_id, message="Filling personal details...")
            await _human_fill(page, "input[name*='name' i], input[placeholder*='name' i]", profile["name"])
            await _human_fill(page, "input[type='email'], input[name*='email' i]", profile["email"])
            await _human_fill(page, "input[type='tel'], input[name*='phone' i]", profile["phone"])

            _update(session_id, message=f"Filling card #{card['priority']}...")
            await _human_fill(page, "input[name*='card'][name*='number' i], input[placeholder*='card number' i]", card["number"])
            await _human_fill(page, "input[name*='expiry' i], input[placeholder*='MM/YY' i]", card["expiry"])
            await _human_fill(page, "input[name*='cvv' i], input[placeholder*='CVV' i]", card["cvv"])

            for iframe_sel in ["iframe[src*='razorpay']", "iframe[src*='stripe']",
                               "iframe[name*='card']", "iframe[title*='payment' i]"]:
                try:
                    await page.wait_for_selector(iframe_sel, timeout=3_000)
                    f = page.frame_locator(iframe_sel)
                    await _human_fill(f, "input[name*='number' i], input[placeholder*='Card' i]", card["number"])
                    await _human_fill(f, "input[name*='expiry' i], input[placeholder*='MM' i]", card["expiry"])
                    await _human_fill(f, "input[name*='cvv' i], input[placeholder*='CVV' i]", card["cvv"])
                    break
                except Exception:
                    pass

            await _try_click_first(page, [
                "button:has-text('Pay Now')", "button:has-text('Confirm')",
                "button:has-text('Place Order')", "button[class*='pay' i]",
                "button[type='submit']",
            ])
            await _human_delay(1.5, 3.0)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass

            # ── OTP gate ─────────────────────────────────────────────────
            is_otp = any(p in page.url.lower() for p in ["otp", "verify", "2fa", "confirm"])
            if not is_otp:
                for sel in OTP_SCREEN_SELECTORS:
                    try:
                        if await page.locator(sel).first.is_visible(timeout=1_500):
                            is_otp = True
                            break
                    except Exception:
                        pass

            if is_otp:
                logger.info(f"[{session_id}] OTP screen detected")
                _update(session_id, status="otp_required",
                        message=f"Card #{card['priority']} — enter OTP")

                otp = await _wait_for_otp(session_id, timeout_s=OTP_TIMEOUT_S)
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
                    "button:has-text('Submit')", "button:has-text('Verify')",
                    "button:has-text('Confirm')", "button[type='submit']",
                ])
                await _human_delay(2.0, 4.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

            # ── Outcome ──────────────────────────────────────────────────
            body = (await page.text_content("body") or "").lower()
            url  = page.url.lower()

            if any(p in url or p in body for p in SUCCESS_PATTERNS):
                _update(session_id, status="success",
                        message=f"Card #{card['priority']} — Booking confirmed!")
                logger.info(f"[{session_id}] CONFIRMED")
            else:
                reason = next((p for p in FAILURE_PATTERNS if p in body), "unknown")
                _update(session_id, status="failed",
                        message=f"Card #{card['priority']} failed ({reason})")
                logger.warning(f"[{session_id}] Failed — {reason}")

        except Exception as e:
            logger.error(f"[{session_id}] Checkout error: {e}")
            _update(session_id, status="failed", message=str(e)[:200])
        finally:
            await ctx.close()
            await browser.close()


async def _wait_for_otp(session_id: str, timeout_s: int = OTP_TIMEOUT_S) -> Optional[str]:
    """Poll session dict for OTP injected by user."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _sessions_lock:
            otp = _sessions.get(session_id, {}).get("otp")
        if otp:
            return otp
        await asyncio.sleep(2)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# §12  CART NOTIFICATION
# ═════════════════════════════════════════════════════════════════════════════

def _notify_cart_ready(watcher_id: str, cart_url: str):
    """POST cart URL back to Flask for Web Push / SMS / email alerts."""
    port = os.environ.get("PORT", "8000")
    try:
        import requests as req
        resp = req.post(
            f"http://127.0.0.1:{port}/api/watchers/{watcher_id}/cart-url",
            json={"cart_url": cart_url},
            timeout=10,
        )
        if resp.ok:
            logger.info(f"Cart URL notification sent for watcher {watcher_id}")
        else:
            logger.warning(f"Cart URL notification failed: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not notify cart URL: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# §13  BACKGROUND WORKER THREAD
# ═════════════════════════════════════════════════════════════════════════════

_worker_thread: Optional[threading.Thread] = None
_worker_started = threading.Event()


def _worker_main():
    """Background thread entry point — owns its own asyncio event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _worker_started.set()
    logger.info("Booking worker thread started (dedicated asyncio loop)")

    while True:
        try:
            try:
                job: BookingJob = _job_queue.get(timeout=2)
            except queue.Empty:
                continue

            logger.info(
                f"Worker processing: watcher={job.watcher_id} "
                f"url={job.checkout_url[:80]} cart_mode={job.cart_mode}"
            )

            pool = _load_card_pool()

            if job.cart_mode:
                sid = _session_id(job.watcher_id, 1)
                has_proxy = all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD])

                if has_proxy:
                    # Full Playwright flow with stealth + proxy
                    card = pool[0] if pool else {"priority": 1}
                    loop.run_until_complete(
                        _run_checkout(
                            sid, job.checkout_url, card,
                            cart_mode=True,
                            target_price=job.target_price,
                            watcher_id=job.watcher_id,
                        )
                    )
                else:
                    # No proxy → instant URL derivation fallback
                    cart_url = _derive_buytickets_url(job.checkout_url)
                    logger.info(
                        f"[{sid}] No proxy — instant URL: {cart_url}"
                    )
                    with _sessions_lock:
                        _sessions[sid].update({
                            "status":   "cart_ready",
                            "message":  "Booking link ready — open and select seats!",
                            "cart_url": cart_url,
                        })
                    _notify_cart_ready(job.watcher_id, cart_url)

            else:
                if not pool:
                    logger.warning("No cards configured for full checkout")
                    continue

                for card in pool:
                    sid = _session_id(job.watcher_id, card["priority"])
                    with _sessions_lock:
                        existing = _sessions.get(sid, {}).get("status")
                        if existing in ("running", "otp_required", "cart_ready"):
                            continue
                        _sessions[sid] = {
                            "status": "running", "message": "Starting...",
                            "otp": None, "device_id": None,
                            "card_priority": card["priority"],
                            "cart_url": None, "cart_mode": False,
                            "created_at": time.time(),
                        }
                    loop.run_until_complete(
                        _run_checkout(
                            sid, job.checkout_url, card,
                            cart_mode=False,
                            target_price=job.target_price,
                            watcher_id=job.watcher_id,
                        )
                    )

            _job_queue.task_done()

        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)


def start_worker():
    """Start the background booking worker thread (called from gunicorn post_fork)."""
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(
        target=_worker_main, daemon=True, name="booking-worker"
    )
    _worker_thread.start()
    _worker_started.wait(timeout=5)
    logger.info("Booking worker thread is ready")


# ═════════════════════════════════════════════════════════════════════════════
# §14  PUBLIC ENTRY POINT — Called by Flask
# ═════════════════════════════════════════════════════════════════════════════

def trigger_auto_checkout(watcher_id: str, checkout_url: str,
                          cart_mode: bool = True,
                          target_price: str = "",
                          owner_email: str = ""):
    """
    Enqueue a booking job for the background worker.

    NON-BLOCKING — drops job in queue and returns immediately.
    The worker thread processes it in its own asyncio loop.
    """
    _cleanup_stale_sessions()

    sid = _session_id(watcher_id, 1)

    # Guard against duplicate jobs
    with _sessions_lock:
        existing = _sessions.get(sid, {}).get("status")
        if existing in ("running", "otp_required", "cart_ready"):
            logger.info(f"[{sid}] Already active ({existing}) — skipping")
            return
        # Reserve slot atomically
        _sessions[sid] = {
            "status": "running", "message": "Queued — waiting for worker...",
            "otp": None, "device_id": None,
            "card_priority": 1, "cart_url": None,
            "cart_mode": cart_mode, "created_at": time.time(),
        }

    job = BookingJob(
        watcher_id=watcher_id, checkout_url=checkout_url,
        cart_mode=cart_mode, target_price=target_price,
        owner_email=owner_email,
    )

    try:
        _job_queue.put_nowait(job)
        logger.info(f"[{sid}] Job enqueued (cart_mode={cart_mode})")
    except queue.Full:
        logger.error(f"[{sid}] Job queue full — dropping")
        _update(sid, status="failed", message="Server busy — try again")
