"""
scraper.py — Headless browser scraper with human-like behaviour.

Strategy:
  • Playwright (Chromium) for JS-heavy pages
  • Requests fallback for lightweight pages
  • Rotating user-agent pool
  • Random delays / realistic scrolling
  • Exponential back-off on failure
"""

import asyncio
import logging
import random
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ticketalert.scraper")

# ── User-Agent pool ───────────────────────────────────────────────────────────
# Real Chrome/Firefox/Edge UAs on Windows, Mac, Android, iOS
USER_AGENTS = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Chrome Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    # Safari iOS
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
]

DESKTOP_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1536, "height": 864},
]

MOBILE_VIEWPORTS = [
    {"width": 390,  "height": 844},   # iPhone 14
    {"width": 412,  "height": 915},   # Pixel 7
    {"width": 360,  "height": 780},   # Galaxy S21
]

# ── Proxy rotation (Bright Data residential) ─────────────────────────────────
# Replace CUSTOMER_ID and PASSWORD with your Bright Data credentials.
# Each entry uses a different session ID so each request rotates the exit IP.
_BRD_HOST = "brd.superproxy.io:22225"
_BRD_USER = "brd-customer-CUSTOMER_ID-zone-residential"
_BRD_PASS = "PASSWORD"

proxies_list = [
    f"http://{_BRD_USER}-session-{i}:{_BRD_PASS}@{_BRD_HOST}"
    for i in range(1, 11)          # 10 pre-seeded session IDs
]

def get_random_proxy():
    proxy = random.choice(proxies_list)
    return {"http": proxy, "https": proxy}


def pick_ua_and_viewport():
    ua = random.choice(USER_AGENTS)
    mobile = "Mobile" in ua or "Android" in ua or "iPhone" in ua
    vp = random.choice(MOBILE_VIEWPORTS if mobile else DESKTOP_VIEWPORTS)
    return ua, vp, mobile


# ── Status keyword lists ──────────────────────────────────────────────────────
SOLD_OUT_PHRASES  = ["sold out", "housefull", "no tickets available",
                     "currently unavailable", "not available", "tickets sold",
                     "show is sold out", "all tickets sold"]
UPCOMING_PHRASES  = ["coming soon", "notify me", "sale starts",
                     "goes on sale", "registration open", "sale opens",
                     "ticket sales open", "sale will begin"]
AVAILABLE_PHRASES = ["book now", "buy now", "buy tickets", "get tickets",
                     "book tickets", "add to cart", "select seats",
                     "choose seats", "proceed", "book", "purchase"]


def _parse_html(html: str, url: str) -> dict:
    """Extract availability status from raw HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True).lower()

    name = ""
    title_tag = soup.find("title")
    if title_tag:
        name = title_tag.get_text(strip=True).split("|")[0].strip()

    price = ""
    price_candidates = soup.find_all(
        attrs={"class": lambda c: c and any(
            k in " ".join(c).lower()
            for k in ["price", "amount", "cost", "ticket-price", "fare"]
        )}
    )
    if price_candidates:
        price = price_candidates[0].get_text(strip=True)[:60]

    for phrase in SOLD_OUT_PHRASES:
        if phrase in text:
            return {"status": "sold_out", "name": name, "price": price}

    for phrase in UPCOMING_PHRASES:
        if phrase in text:
            return {"status": "upcoming", "name": name, "price": price}

    buttons = soup.find_all(["button", "a"])
    for btn in buttons:
        btn_text = btn.get_text(strip=True).lower()
        for phrase in AVAILABLE_PHRASES:
            if phrase in btn_text:
                disabled = (
                    btn.get("disabled") is not None
                    or "disabled" in (btn.get("class") or [])
                    or btn.get("aria-disabled") == "true"
                )
                if not disabled:
                    return {"status": "available", "name": name, "price": price}

    for phrase in AVAILABLE_PHRASES:
        if phrase in text:
            return {"status": "available", "name": name, "price": price}

    return {"status": "unknown", "name": name, "price": price}


# ── Playwright async scraper ──────────────────────────────────────────────────

async def _fetch_with_playwright(url: str) -> Optional[str]:
    """
    Open url in a headless Chromium browser, simulate human scrolling,
    wait for network idle, then return the page HTML.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning("Playwright not installed — falling back to requests")
        return None

    ua, viewport, is_mobile = pick_ua_and_viewport()

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
            user_agent=ua,
            viewport=viewport,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            is_mobile=is_mobile,
            has_touch=is_mobile,
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )

        # Mask navigator.webdriver
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        try:
            # Throttle: random pause before navigation (1.5 – 4.5 s)
            await asyncio.sleep(random.uniform(1.5, 4.5))

            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)

            # Wait for network to mostly settle (up to 5 s extra)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PWTimeout:
                pass  # fine — proceed with what we have

            # Human-like scroll down the page
            height = await page.evaluate("document.body.scrollHeight")
            scroll_steps = random.randint(3, 6)
            for i in range(1, scroll_steps + 1):
                target_y = int((height / scroll_steps) * i)
                await page.evaluate(f"window.scrollTo(0, {target_y})")
                await asyncio.sleep(random.uniform(0.3, 0.9))

            # Brief pause at the bottom before grabbing HTML
            await asyncio.sleep(random.uniform(0.5, 1.5))

            html = await page.content()
            return html

        except PWTimeout:
            logger.warning(f"Playwright timeout on {url}")
            return None
        except Exception as e:
            logger.error(f"Playwright error on {url}: {e}")
            return None
        finally:
            await context.close()
            await browser.close()


# ── Requests fallback ─────────────────────────────────────────────────────────

def _fetch_with_requests(url: str) -> Optional[str]:
    ua, _, _ = pick_ua_and_viewport()
    headers = {
        "User-Agent": ua,
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
    }
    # Throttle: random pause before request (1 – 3 s)
    time.sleep(random.uniform(1.0, 3.0))
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True,
                            proxies=get_random_proxy() if proxies_list else None)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Requests error on {url}: {e}")
        return None


# ── CAPTCHA solving ───────────────────────────────────────────────────────────

def solve_recaptcha(site_key: str, page_url: str, api_key: str = "") -> Optional[str]:
    """Submit a reCAPTCHA to 2captcha and poll for the token."""
    submit = requests.post("https://2captcha.com/in.php", data={
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
    })
    try:
        task_id = submit.text.split("|")[1]
    except IndexError:
        logger.error(f"2captcha submission failed: {submit.text}")
        return None

    time.sleep(15)
    result = requests.get(
        f"https://2captcha.com/res.php",
        params={"key": api_key, "action": "get", "id": task_id},
    )
    try:
        return result.text.split("|")[1]
    except IndexError:
        logger.error(f"2captcha result failed: {result.text}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def check_url_availability(url: str, use_browser: bool = True) -> dict:
    """
    Check ticket availability for a URL.
    Tries Playwright first (handles JS-rendered pages), falls back to requests.
    Retries up to 2 times with exponential back-off.
    """
    max_retries = 2
    backoff = 3.0

    for attempt in range(max_retries + 1):
        try:
            html = None

            if use_browser:
                try:
                    html = asyncio.run(_fetch_with_playwright(url))
                except RuntimeError:
                    # Already inside a running event loop (shouldn't happen with threading)
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    html = loop.run_until_complete(_fetch_with_playwright(url))
                    loop.close()

            if html is None:
                html = _fetch_with_requests(url)

            if html is None:
                raise ValueError("Both Playwright and requests returned no content")

            return _parse_html(html, url)

        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}")
            if attempt < max_retries:
                sleep_time = backoff * (2 ** attempt) + random.uniform(0, 2)
                logger.info(f"Retrying in {sleep_time:.1f}s…")
                time.sleep(sleep_time)
            else:
                logger.error(f"All {max_retries + 1} attempts failed for {url}")
                return {"status": "error", "name": "", "price": "",
                        "error": str(e)}
