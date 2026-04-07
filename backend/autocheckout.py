"""
autocheckout.py — Headless auto-checkout for BookMyShow / District.

Triggered by the monitor loop when a watcher transitions to "available".
Fills all details automatically and pauses only at the OTP screen.
OTP is injected via the /api/submit-otp endpoint (user types it on the
TicketAlert page — no manual browser interaction needed).
"""

import asyncio
import logging
import os
import random
import time
from typing import Optional

logger = logging.getLogger("ticketalert.checkout")

# ── Shared session state ──────────────────────────────────────────────────────
# Keyed by watcher_id. Written by this module, read by app.py endpoints.
#
# Shape: {
#   "status":  "running" | "otp_required" | "success" | "failed" | "idle",
#   "message": str,
#   "otp":     str | None,          # set externally by /api/submit-otp
# }
_sessions: dict = {}


def get_session(watcher_id: str) -> dict:
    return _sessions.get(watcher_id, {"status": "idle", "message": "", "otp": None})


def inject_otp(watcher_id: str, otp: str):
    """Called by /api/submit-otp to deliver the OTP to the waiting automation."""
    if watcher_id in _sessions:
        _sessions[watcher_id]["otp"] = otp
        logger.info(f"[{watcher_id}] OTP injected")


# ── Profile (from env vars) ───────────────────────────────────────────────────

def _profile():
    return {
        "name":     os.environ.get("PROFILE_NAME",  ""),
        "email":    os.environ.get("PROFILE_EMAIL", ""),
        "phone":    os.environ.get("PROFILE_PHONE", ""),
        "card_no":  os.environ.get("CARD_NUMBER",   ""),
        "card_exp": os.environ.get("CARD_EXPIRY",   ""),   # MM/YY
        "card_cvv": os.environ.get("CARD_CVV",      ""),
    }


# ── Selector sets — add/adjust as BookMyShow updates its DOM ─────────────────

PROCEED_SELECTORS = [
    "button[class*='proceed' i]",
    "button[class*='Proceed' i]",
    "a[class*='proceed' i]",
    "button:has-text('Proceed')",
    "button:has-text('Continue')",
    "button:has-text('Book')",
]

OTP_SCREEN_SELECTORS = [
    "input[placeholder*='OTP' i]",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[autocomplete='one-time-code']",
    "[class*='otp' i] input",
]

OTP_URL_PATTERNS = ["otp", "verify", "authenticate", "2fa", "confirm"]

SUCCESS_PATTERNS  = ["confirmed", "success", "booking-confirmed", "thank-you",
                     "booking confirmed", "order confirmed"]
FAILURE_PATTERNS  = ["failed", "declined", "error", "invalid", "expired",
                     "could not", "try again"]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _rand_delay(lo=0.3, hi=0.9):
    await asyncio.sleep(random.uniform(lo, hi))


async def _safe_fill(page, selector: str, value: str, timeout=6_000):
    """Fill a field if visible; silent no-op otherwise."""
    if not value:
        return
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.fill(value)
        await _rand_delay(0.1, 0.3)
    except Exception:
        pass


async def _safe_click(page, selector: str, timeout=6_000):
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        # Small human-like delay before click
        await _rand_delay(0.2, 0.5)
        await loc.click()
        return True
    except Exception:
        return False


async def _click_first_match(page, selectors: list, timeout=8_000) -> bool:
    for sel in selectors:
        if await _safe_click(page, sel, timeout):
            logger.info(f"Clicked: {sel}")
            return True
    return False


async def _scroll_page(page):
    height = await page.evaluate("document.body.scrollHeight")
    steps = random.randint(3, 5)
    for i in range(1, steps + 1):
        await page.evaluate(f"window.scrollTo(0, {int(height * i / steps)})")
        await _rand_delay(0.2, 0.5)


# ── OTP gate ──────────────────────────────────────────────────────────────────

async def _wait_for_otp(watcher_id: str, timeout_s=300) -> Optional[str]:
    """
    Poll _sessions[watcher_id]['otp'] until it is set or timeout expires.
    Returns the OTP string or None on timeout.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        otp = _sessions.get(watcher_id, {}).get("otp")
        if otp:
            return otp
        await asyncio.sleep(2)
    return None


async def _is_otp_screen(page) -> bool:
    url = page.url.lower()
    if any(p in url for p in OTP_URL_PATTERNS):
        return True
    for sel in OTP_SCREEN_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1_500):
                return True
        except Exception:
            pass
    return False


# ── Main checkout coroutine ───────────────────────────────────────────────────

async def _run_checkout(watcher_id: str, checkout_url: str):
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.error("Playwright not available — cannot auto-checkout")
        _sessions[watcher_id]["status"]  = "failed"
        _sessions[watcher_id]["message"] = "Playwright not installed"
        return

    profile = _profile()
    _sessions[watcher_id] = {"status": "running", "message": "Starting checkout…", "otp": None}

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
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
                "DNT": "1",
            },
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        try:
            # ── 1. Navigate ───────────────────────────────────────────────────
            logger.info(f"[{watcher_id}] Navigating to {checkout_url}")
            _sessions[watcher_id]["message"] = "Navigating to checkout…"
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await _scroll_page(page)

            # ── 2. Seat / quantity selection (BookMyShow step) ────────────────
            _sessions[watcher_id]["message"] = "Selecting seats…"
            # Try clicking the best/cheapest available seat category
            seat_selectors = [
                "[class*='price-card']:not([class*='sold-out']):first-child",
                "[class*='category']:not([class*='disabled']):first-child",
                "[class*='ticket-type']:first-child",
                "li[class*='list']:not([class*='sold']):first-child",
            ]
            for sel in seat_selectors:
                if await _safe_click(page, sel, timeout=4_000):
                    await _rand_delay(0.5, 1.0)
                    break

            # Quantity: try to set max allowed (up to 6 on BMS)
            qty_selectors = [
                "select[id*='qty']", "select[name*='qty']",
                "input[type='number']",
                "[class*='quantity'] select",
            ]
            for sel in qty_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3_000):
                        # Try to select max value
                        opts = await loc.evaluate(
                            "el => [...el.options || []].map(o => o.value)"
                        )
                        if opts:
                            await loc.select_option(opts[-1])
                        await _rand_delay(0.3, 0.6)
                        break
                except Exception:
                    pass

            # Click Proceed / Book
            await _click_first_match(page, PROCEED_SELECTORS)
            await _rand_delay(1.0, 2.0)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)

            # ── 3. Fill personal details ──────────────────────────────────────
            _sessions[watcher_id]["message"] = "Filling personal details…"
            await _safe_fill(page, "input[name*='name' i], input[id*='name' i], input[placeholder*='name' i]",  profile["name"])
            await _safe_fill(page, "input[type='email'], input[name*='email' i]",                                profile["email"])
            await _safe_fill(page, "input[type='tel'],   input[name*='phone' i], input[name*='mobile' i]",       profile["phone"])

            # ── 4. Fill payment details ───────────────────────────────────────
            _sessions[watcher_id]["message"] = "Filling payment details…"
            # Try inline card fields first
            await _safe_fill(page, "input[name*='card' i][name*='number' i], input[placeholder*='card number' i]", profile["card_no"])
            await _safe_fill(page, "input[name*='expiry' i], input[placeholder*='MM/YY' i], input[name*='exp' i]", profile["card_exp"])
            await _safe_fill(page, "input[name*='cvv' i], input[placeholder*='CVV' i], input[name*='cvc' i]",      profile["card_cvv"])

            # Try payment iframe (Razorpay / Stripe style)
            for iframe_sel in ["iframe[src*='razorpay']", "iframe[src*='stripe']",
                               "iframe[name*='card']",    "iframe[title*='payment' i]"]:
                try:
                    await page.wait_for_selector(iframe_sel, timeout=4_000)
                    f = page.frame_locator(iframe_sel)
                    await _safe_fill(f, "input[name*='number' i], input[placeholder*='Card number' i]", profile["card_no"])
                    await _safe_fill(f, "input[name*='expiry' i], input[placeholder*='MM' i]",          profile["card_exp"])
                    await _safe_fill(f, "input[name*='cvv' i], input[placeholder*='CVV' i]",            profile["card_cvv"])
                    break
                except Exception:
                    pass

            # Click confirm / pay
            await _click_first_match(page, [
                "button:has-text('Pay Now')",
                "button:has-text('Confirm')",
                "button:has-text('Place Order')",
                "button[class*='pay' i]",
                "button[class*='confirm' i]",
                "button[type='submit']",
            ])
            await _rand_delay(1.5, 3.0)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)

            # ── 5. OTP gate ───────────────────────────────────────────────────
            if await _is_otp_screen(page):
                logger.info(f"[{watcher_id}] OTP screen detected — waiting for user input")
                _sessions[watcher_id]["status"]  = "otp_required"
                _sessions[watcher_id]["message"] = "Enter OTP on TicketAlert to complete booking"

                otp = await _wait_for_otp(watcher_id, timeout_s=300)
                if not otp:
                    raise TimeoutError("OTP not received within 5 minutes")

                logger.info(f"[{watcher_id}] OTP received — submitting")
                _sessions[watcher_id]["message"] = "Submitting OTP…"

                # Fill OTP field
                for sel in OTP_SCREEN_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if await loc.is_visible(timeout=2_000):
                            await loc.fill(otp)
                            await _rand_delay(0.3, 0.7)
                            break
                    except Exception:
                        pass

                # Submit
                await _click_first_match(page, [
                    "button:has-text('Submit')",
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button[type='submit']",
                ])
                await _rand_delay(2.0, 4.0)
                await page.wait_for_load_state("networkidle", timeout=15_000)

            # ── 6. Determine outcome ──────────────────────────────────────────
            final_url  = page.url.lower()
            final_text = (await page.text_content("body") or "").lower()

            if any(p in final_url or p in final_text for p in SUCCESS_PATTERNS):
                _sessions[watcher_id]["status"]  = "success"
                _sessions[watcher_id]["message"] = "Booking confirmed!"
                logger.info(f"[{watcher_id}] BOOKING CONFIRMED")
            else:
                failed_reason = next((p for p in FAILURE_PATTERNS if p in final_text), "unknown")
                _sessions[watcher_id]["status"]  = "failed"
                _sessions[watcher_id]["message"] = f"Booking failed ({failed_reason})"
                logger.warning(f"[{watcher_id}] Booking failed — {failed_reason}")

        except Exception as e:
            logger.error(f"[{watcher_id}] Checkout error: {e}")
            _sessions[watcher_id]["status"]  = "failed"
            _sessions[watcher_id]["message"] = str(e)
        finally:
            await context.close()
            await browser.close()


# ── Public entry point (called from app.py in a thread) ──────────────────────

def trigger_auto_checkout(watcher_id: str, checkout_url: str):
    """
    Spawn the checkout coroutine in its own event loop (called from a
    daemon thread so it doesn't block the Flask server).
    """
    if _sessions.get(watcher_id, {}).get("status") in ("running", "otp_required"):
        logger.info(f"[{watcher_id}] Checkout already in progress — skipping")
        return

    logger.info(f"[{watcher_id}] Triggering auto-checkout for {checkout_url}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_checkout(watcher_id, checkout_url))
    finally:
        loop.close()
