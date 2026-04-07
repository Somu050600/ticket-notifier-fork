"""
autocheckout.py — Multi-card, multi-device headless auto-checkout.

Card pool (priority order):
  CARD_1_NUMBER / CARD_1_EXPIRY / CARD_1_CVV / CARD_1_NAME  ← highest priority
  CARD_2_NUMBER / CARD_2_EXPIRY / CARD_2_CVV / CARD_2_NAME
  CARD_3_NUMBER / CARD_3_EXPIRY / CARD_3_CVV / CARD_3_NAME

When availability is detected:
  - One checkout session per configured card is started in parallel.
  - Each device/browser that opens TicketAlert claims a slot (first-come,
    first-served) and handles OTP for its assigned card.
  - Session IDs: "{watcher_id}-slot-{1|2|3}"
"""

import asyncio
import logging
import os
import random
import threading
import time
from typing import Optional

logger = logging.getLogger("ticketalert.checkout")

# ── Card pool ─────────────────────────────────────────────────────────────────

def _load_card_pool() -> list[dict]:
    """
    Returns a list of card dicts in priority order (index 0 = highest priority).
    Falls back to the legacy CARD_NUMBER / CARD_EXPIRY / CARD_CVV env vars for
    Card 1 if the numbered vars are absent.
    """
    pool = []
    for n in range(1, 4):          # slots 1, 2, 3
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
        if number:   # only include if a card number is configured
            pool.append({
                "priority": n,
                "number":   number,
                "expiry":   expiry,
                "cvv":      cvv,
                "name":     name,
            })
    return pool


def _profile() -> dict:
    return {
        "name":  os.environ.get("PROFILE_NAME",  ""),
        "email": os.environ.get("PROFILE_EMAIL", ""),
        "phone": os.environ.get("PROFILE_PHONE", ""),
    }


# ── Session state ─────────────────────────────────────────────────────────────
# Keyed by session_id = "{watcher_id}-slot-{n}"
#
# Shape per session:
# {
#   "status":    "running"|"otp_required"|"success"|"failed"|"idle",
#   "message":   str,
#   "otp":       str | None,   # set by inject_otp() when user submits
#   "device_id": str | None,   # set by claim_slot()
#   "card_priority": int,
# }
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _session_id(watcher_id: str, priority: int) -> str:
    return f"{watcher_id}-slot-{priority}"


# ── Public API (called from app.py) ──────────────────────────────────────────

def claim_slot(watcher_id: str, device_id: str) -> Optional[str]:
    """
    Assigns the next unclaimed active slot to `device_id`.
    Returns the session_id the device should use, or None if no slot is
    available (all claimed or no active sessions for this watcher).
    """
    with _sessions_lock:
        # Find the lowest-priority unclaimed session for this watcher
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("status") in ("running", "otp_required") \
                    and sess.get("device_id") is None:
                sess["device_id"] = device_id
                logger.info(f"[{sid}] Claimed by device {device_id}")
                return sid
        # Already claimed? Let device re-claim its own existing session
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return sid
    return None


def get_session(session_id: str) -> dict:
    with _sessions_lock:
        sess = _sessions.get(session_id, {})
    return {
        "status":        sess.get("status", "idle"),
        "message":       sess.get("message", ""),
        "card_priority": sess.get("card_priority", 0),
        "device_id":     sess.get("device_id"),
    }


def get_session_for_device(watcher_id: str, device_id: str) -> dict:
    """Returns the session that belongs to this device, or idle if none."""
    with _sessions_lock:
        for priority in range(1, 4):
            sid = _session_id(watcher_id, priority)
            sess = _sessions.get(sid)
            if sess and sess.get("device_id") == device_id:
                return {**get_session(sid), "session_id": sid}
    return {"status": "idle", "message": "", "session_id": None}


def inject_otp(session_id: str, otp: str):
    """Delivers OTP from the user to the waiting automation."""
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id]["otp"] = otp
            logger.info(f"[{session_id}] OTP injected")


# ── Checkout coroutine ────────────────────────────────────────────────────────

PROCEED_SELECTORS = [
    "button:has-text('Proceed')",
    "button:has-text('Continue')",
    "button:has-text('Book')",
    "button[class*='proceed' i]",
    "a[class*='proceed' i]",
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


async def _rand(lo=0.3, hi=0.9):
    await asyncio.sleep(random.uniform(lo, hi))


async def _fill(scope, selector: str, value: str, timeout=6_000):
    if not value:
        return
    try:
        loc = scope.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.fill(value)
        await _rand(0.1, 0.3)
    except Exception:
        pass


async def _click(page, selector: str, timeout=6_000) -> bool:
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await _rand(0.2, 0.5)
        await loc.click()
        return True
    except Exception:
        return False


async def _click_first(page, selectors: list, timeout=8_000) -> bool:
    for sel in selectors:
        if await _click(page, sel, timeout):
            return True
    return False


async def _is_otp_screen(page) -> bool:
    if any(p in page.url.lower() for p in OTP_URL_PATTERNS):
        return True
    for sel in OTP_SCREEN_SELECTORS:
        try:
            if await page.locator(sel).first.is_visible(timeout=1_500):
                return True
        except Exception:
            pass
    return False


async def _wait_for_otp(session_id: str, timeout_s=300) -> Optional[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _sessions_lock:
            otp = _sessions.get(session_id, {}).get("otp")
        if otp:
            return otp
        await asyncio.sleep(2)
    return None


def _update(session_id: str, **kwargs):
    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id].update(kwargs)


async def _select_seat_by_price(page, target_price: str) -> bool:
    """
    Finds and clicks the seat category whose price text best matches target_price.
    Returns True if a matching category was clicked, False otherwise.

    Matching strategy:
      1. Exact numeric match  (e.g. "1500" in "₹1,500")
      2. Nearest price above  (next tier up, as a fallback)
    """
    if not target_price:
        return False

    # Normalise target to a plain integer (strip ₹, commas, spaces)
    target_num = int("".join(filter(str.isdigit, target_price)) or "0")
    if not target_num:
        return False

    # Collect all visible seat-category elements with a price
    category_selectors = [
        "[class*='price-card']:not([class*='sold-out'])",
        "[class*='category']:not([class*='disabled']):not([class*='sold'])",
        "[class*='seat-type']:not([class*='unavailable'])",
        "li[class*='ticket']:not([class*='sold'])",
    ]

    for container_sel in category_selectors:
        try:
            cards = await page.locator(container_sel).all()
            if not cards:
                continue

            candidates = []
            for card_el in cards:
                text = (await card_el.text_content() or "").replace(",", "")
                digits = "".join(filter(str.isdigit, text))
                if digits:
                    price_val = int(digits)
                    candidates.append((price_val, card_el))

            if not candidates:
                continue

            # Try exact match first
            for price_val, el in candidates:
                if price_val == target_num:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    logger.info(f"Seat selected: ₹{price_val} (exact match)")
                    return True

            # Fall back to nearest price >= target
            above = [(v, el) for v, el in candidates if v >= target_num]
            if above:
                best_val, best_el = min(above, key=lambda x: x[0])
                await best_el.scroll_into_view_if_needed()
                await best_el.click()
                logger.info(f"Seat selected: ₹{best_val} (nearest above ₹{target_num})")
                return True

        except Exception:
            continue

    logger.warning(f"No seat category found matching ₹{target_num}")
    return False


async def _capture_cart_url(page) -> str | None:
    """
    Attempts to extract the shareable cart/checkout URL from the current page.
    Tries: current URL, clipboard API, hidden share-link elements.
    """
    url = page.url
    # BookMyShow cart URLs typically contain 'cart' or 'checkout'
    if any(k in url.lower() for k in ["cart", "checkout", "order", "booking"]):
        return url
    # Try looking for a share / copy-link element
    for sel in ["[class*='cart-url']", "[data-cart-url]", "input[value*='cart']"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2_000):
                val = await el.get_attribute("value") or await el.text_content()
                if val and val.startswith("http"):
                    return val.strip()
        except Exception:
            pass
    return url   # fall back to current URL


def _notify_cart_ready(watcher_id: str, cart_url: str):
    """Posts cart URL back to Flask so it can push-notify the user."""
    try:
        import requests as req
        req.post(
            f"http://localhost:{os.environ.get('PORT', 5000)}/api/watchers/{watcher_id}/cart-url",
            json={"cart_url": cart_url},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Could not notify cart URL: {e}")


async def _run_checkout(session_id: str, checkout_url: str, card: dict,
                        cart_mode: bool = True, target_price: str = "",
                        watcher_id: str = ""):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed")
        _update(session_id, status="failed", message="Playwright not installed")
        return

    profile = _profile()
    mode_label = "cart" if cart_mode else "checkout"
    _update(session_id, status="running", message=f"Starting {mode_label}…")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            window.chrome = { runtime: {} };
        """)
        page = await ctx.new_page()

        try:
            # ── 1. Navigate ───────────────────────────────────────────────────
            logger.info(f"[{session_id}] Navigating → {checkout_url}")
            _update(session_id, message="Navigating…")
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=10_000)

            # ── 2. Seat selection by price (if target_price specified) ────────
            _update(session_id, message=f"Selecting seats{' at ₹'+target_price if target_price else ''}…")
            seat_picked = False
            if target_price:
                seat_picked = await _select_seat_by_price(page, target_price)

            # Fallback: pick cheapest available category if no price target or match
            if not seat_picked:
                for sel in [
                    "[class*='price-card']:not([class*='sold-out']):first-child",
                    "[class*='category']:not([class*='disabled']):first-child",
                    "li[class*='list']:not([class*='sold']):first-child",
                ]:
                    if await _click(page, sel, timeout=3_000):
                        await _rand(0.4, 0.8)
                        break

            # Set max quantity
            for sel in ["select[id*='qty']", "select[name*='qty']",
                        "input[type='number']", "[class*='quantity'] select"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2_000):
                        opts = await loc.evaluate(
                            "el => [...(el.options||[])].map(o=>o.value)"
                        )
                        if opts:
                            await loc.select_option(opts[-1])
                        await _rand(0.2, 0.5)
                        break
                except Exception:
                    pass

            await _click_first(page, PROCEED_SELECTORS)
            await _rand(1.0, 2.0)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)

            # ── 3. Personal details ───────────────────────────────────────────
            _update(session_id, message="Filling personal details…")
            await _fill(page, "input[name*='name' i], input[placeholder*='name' i]", profile["name"])
            await _fill(page, "input[type='email'], input[name*='email' i]",          profile["email"])
            await _fill(page, "input[type='tel'],   input[name*='phone' i]",          profile["phone"])

            # ── 4a. CART MODE — stop here, capture URL, notify user ───────────
            if cart_mode:
                _update(session_id, message="Capturing cart URL…")
                await _rand(1.0, 2.0)
                cart_url = await _capture_cart_url(page)
                _update(session_id,
                        status="cart_ready",
                        message="Cart is ready — tap the link to pay",
                        cart_url=cart_url)
                logger.info(f"[{session_id}] Cart URL: {cart_url}")
                if watcher_id:
                    _notify_cart_ready(watcher_id, cart_url)
                return   # ← hand off to user for payment

            # ── 4b. FULL CHECKOUT — fill card and pay ─────────────────────────
            _update(session_id, message=f"Filling card #{card['priority']}…")
            await _fill(
                page,
                "input[name*='card'][name*='number' i], input[placeholder*='card number' i]",
                card["number"],
            )
            await _fill(
                page,
                "input[name*='expiry' i], input[placeholder*='MM/YY' i]",
                card["expiry"],
            )
            await _fill(
                page,
                "input[name*='cvv' i], input[placeholder*='CVV' i]",
                card["cvv"],
            )
            for iframe_sel in ["iframe[src*='razorpay']", "iframe[src*='stripe']",
                               "iframe[name*='card']",    "iframe[title*='payment' i]"]:
                try:
                    await page.wait_for_selector(iframe_sel, timeout=3_000)
                    f = page.frame_locator(iframe_sel)
                    await _fill(f, "input[name*='number' i], input[placeholder*='Card number' i]", card["number"])
                    await _fill(f, "input[name*='expiry' i], input[placeholder*='MM' i]",          card["expiry"])
                    await _fill(f, "input[name*='cvv' i],    input[placeholder*='CVV' i]",          card["cvv"])
                    break
                except Exception:
                    pass

            # Click Pay
            await _click_first(page, [
                "button:has-text('Pay Now')",
                "button:has-text('Confirm')",
                "button:has-text('Place Order')",
                "button[class*='pay' i]",
                "button[class*='confirm' i]",
                "button[type='submit']",
            ])
            await _rand(1.5, 3.0)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)

            # ── 6. OTP gate ───────────────────────────────────────────────────
            if await _is_otp_screen(page):
                logger.info(f"[{session_id}] OTP screen — waiting for user input")
                _update(
                    session_id,
                    status="otp_required",
                    message=f"Card #{card['priority']} — enter OTP to confirm",
                )

                otp = await _wait_for_otp(session_id, timeout_s=300)
                if not otp:
                    raise TimeoutError("OTP not received within 5 minutes")

                _update(session_id, message="Submitting OTP…")
                for sel in OTP_SCREEN_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=2_000):
                            await loc.fill(otp)
                            await _rand(0.3, 0.7)
                            break
                    except Exception:
                        pass

                await _click_first(page, [
                    "button:has-text('Submit')",
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button[type='submit']",
                ])
                await _rand(2.0, 4.0)
                await page.wait_for_load_state("networkidle", timeout=15_000)

            # ── 7. Outcome ────────────────────────────────────────────────────
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
                logger.warning(f"[{session_id}] failed — {reason}")

        except Exception as e:
            logger.error(f"[{session_id}] error: {e}")
            _update(session_id, status="failed", message=str(e))
        finally:
            await ctx.close()
            await browser.close()


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


def trigger_auto_checkout(watcher_id: str, checkout_url: str,
                          cart_mode: bool = True,
                          target_price: str = "",
                          owner_email: str = ""):
    """
    Starts one session per configured card (highest priority first).
    cart_mode=True  → adds to cart and sends cart URL to user (default).
    cart_mode=False → full checkout including card fill and OTP.
    target_price    → e.g. "1500" selects that seat tier automatically.
    """
    pool = _load_card_pool()
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
                "message":       "Starting…",
                "otp":           None,
                "device_id":     None,
                "card_priority": card["priority"],
                "cart_url":      None,
                "cart_mode":     cart_mode,
            }

        logger.info(f"[{sid}] Launching {'cart' if cart_mode else 'checkout'} "
                    f"with card #{card['priority']}"
                    + (f" targeting ₹{target_price}" if target_price else ""))
        threading.Thread(
            target=_run_in_thread,
            args=(sid, checkout_url, card, cart_mode, target_price, watcher_id),
            daemon=True,
        ).start()
