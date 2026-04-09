"""
scraper.py — Availability checker with residential proxy & stealth.

Strategy (in order of preference):
  1. BookMyShow API — direct JSON endpoint, no browser needed, fastest
  2. Playwright + stealth + residential proxy — for JS-heavy pages
  3. Requests with proxy — lightweight fallback

The BMS API approach is critical: instead of rendering the full page and
parsing HTML for "Book Now" text, we hit BMS's own internal API that
returns event data as JSON.  This is 10x faster and immune to HTML
layout changes.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ticketalert.scraper")

# ── Proxy config (same env vars as autocheckout.py) ──────────────────────────
PROXY_SERVER   = os.environ.get("PROXY_SERVER", "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")

# ── User-Agent pool ──────────────────────────────────────────────────────────
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


def _get_requests_proxy() -> Optional[dict]:
    """Build requests-compatible proxy dict with sticky session."""
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        return None
    session_id = uuid.uuid4().hex[:8]
    sticky_user = f"{PROXY_USERNAME}-session-{session_id}"
    proxy_url = f"http://{sticky_user}:{PROXY_PASSWORD}@{PROXY_SERVER}"
    return {"http": proxy_url, "https": proxy_url}


def _get_playwright_proxy() -> Optional[dict]:
    """Build Playwright-compatible proxy dict with sticky session."""
    if not all([PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD]):
        return None
    session_id = uuid.uuid4().hex[:8]
    return {
        "server":   f"http://{PROXY_SERVER}",
        "username": f"{PROXY_USERNAME}-session-{session_id}",
        "password": PROXY_PASSWORD,
    }


# ── Status keyword lists ────────────────────────────────────────────────────
SOLD_OUT_PHRASES = [
    "sold out", "housefull", "no tickets available",
    "currently unavailable", "not available", "tickets sold",
    "show is sold out", "all tickets sold", "event closed",
    "booking closed", "bookings closed", "sales closed",
    "sale closed", "event is closed", "currently not on sale",
]
UPCOMING_PHRASES = [
    "coming soon", "notify me", "sale starts",
    "goes on sale", "registration open", "sale opens",
    "ticket sales open", "sale will begin",
]
AVAILABLE_PHRASES = [
    "book now", "buy now", "buy tickets", "get tickets",
    "book tickets", "add to cart", "select seats",
    "choose seats", "filling fast",
]


# ═════════════════════════════════════════════════════════════════════════════
# §1  BOOKMYSHOW API CHECKER (fastest, no browser needed)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_bms_event_code(url: str) -> Optional[str]:
    """
    Extract the ET code from a BookMyShow URL.
    e.g. .../kolkata-knight-riders-vs.../ET00493084 → ET00493084
    """
    m = re.search(r"(ET\d{6,12})", url)
    return m.group(1) if m else None


def _check_bms_api(url: str) -> Optional[dict]:
    """
    Hit BookMyShow's internal event data API directly.
    This returns JSON with event status, pricing, and availability
    without needing to render the full page.

    Returns a parsed result dict or None if the API call fails.
    """
    event_code = _extract_bms_event_code(url)
    if not event_code:
        return None

    # BMS serves event data via multiple API patterns. Try them in order.
    api_urls = [
        # Primary: event detail API (returns JSON with ShowDetails, pricing)
        f"https://in.bookmyshow.com/api/explore/v1/discover/event/{event_code}",
        # Alternative: serp/venue data
        f"https://in.bookmyshow.com/api/event-home/v1/event/{event_code}",
    ]

    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://in.bookmyshow.com/",
        "Origin": "https://in.bookmyshow.com",
        "DNT": "1",
    }

    proxy = _get_requests_proxy()

    for api_url in api_urls:
        try:
            resp = requests.get(
                api_url,
                headers=headers,
                proxies=proxy,
                timeout=12,
                allow_redirects=True,
            )

            if resp.status_code != 200:
                logger.debug(f"BMS API {resp.status_code}: {api_url[:80]}")
                continue

            data = resp.json()

            # Extract event name
            name = ""
            if isinstance(data, dict):
                name = (
                    data.get("EventTitle", "")
                    or data.get("EventName", "")
                    or data.get("title", "")
                    or data.get("name", "")
                )
                # Try nested structures
                if not name:
                    for key in ["event", "data", "result"]:
                        nested = data.get(key, {})
                        if isinstance(nested, dict):
                            name = nested.get("EventTitle", "") or nested.get("name", "")
                            if name:
                                break

            # Extract price
            price = ""
            price_val = (
                data.get("MinPrice", "")
                or data.get("fmtdMinPrice", "")
                or data.get("price", "")
            )
            if price_val:
                price = f"₹{price_val} onwards"

            # Determine status from response content
            text = json.dumps(data).lower()

            for phrase in SOLD_OUT_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → sold_out")
                    return {"status": "sold_out", "name": name, "price": price}

            for phrase in UPCOMING_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → upcoming")
                    return {"status": "upcoming", "name": name, "price": price}

            # Check for positive signals
            has_shows = False
            if isinstance(data, dict):
                # Check various indicators that tickets exist and are bookable
                show_details = data.get("ShowDetails", data.get("childEvents", []))
                if show_details:
                    has_shows = True
                if data.get("BookMyShow", "") or data.get("isBookable"):
                    has_shows = True
                if data.get("MinPrice") or data.get("fmtdMinPrice"):
                    has_shows = True

            for phrase in AVAILABLE_PHRASES:
                if phrase in text:
                    logger.info(f"BMS API: {event_code} → available (phrase: {phrase})")
                    return {"status": "available", "name": name, "price": price}

            if has_shows:
                logger.info(f"BMS API: {event_code} → available (has shows/pricing)")
                return {"status": "available", "name": name, "price": price}

            logger.info(f"BMS API: {event_code} → unknown (no clear signals)")
            return None  # inconclusive — let other methods try

        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            logger.debug(f"BMS API failed for {api_url[:60]}: {e}")
            continue

    return None


# ═════════════════════════════════════════════════════════════════════════════
# §2  REQUESTS-BASED HTML CHECKER (with proxy)
# ═════════════════════════════════════════════════════════════════════════════

def _parse_html(html: str, url: str) -> dict:
    """Extract availability status from raw HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True).lower()

    # Extract event name from <title>
    name = ""
    title_tag = soup.find("title")
    if title_tag:
        raw_title = title_tag.get_text(strip=True)
        # Remove " - BookMyShow" suffix and similar
        name = re.split(r"\s*[\|–—]\s*", raw_title)[0].strip()

    # Extract price
    price = ""
    price_candidates = soup.find_all(
        attrs={"class": lambda c: c and any(
            k in " ".join(c).lower()
            for k in ["price", "amount", "cost", "ticket-price", "fare"]
        )}
    )
    if price_candidates:
        price = price_candidates[0].get_text(strip=True)[:60]

    # Also check for "₹XXXX onwards" pattern in text
    if not price:
        price_match = re.search(r"₹[\d,]+\s*onwards", text)
        if price_match:
            price = price_match.group(0)

    # Check sold out first
    for phrase in SOLD_OUT_PHRASES:
        if phrase in text:
            return {"status": "sold_out", "name": name, "price": price}

    # Check upcoming
    for phrase in UPCOMING_PHRASES:
        if phrase in text:
            return {"status": "upcoming", "name": name, "price": price}

    # Check buttons for availability
    buttons = soup.find_all(["button", "a"])
    for btn in buttons:
        btn_text = btn.get_text(strip=True).lower()
        classes = btn.get("class") or []
        class_text = " ".join(classes).lower() if isinstance(classes, list) else str(classes).lower()
        aria_disabled = str(btn.get("aria-disabled", "")).lower()
        disabled = (
            btn.get("disabled") is not None
            or "disabled" in class_text
            or "inactive" in class_text
            or "closed" in class_text
            or aria_disabled == "true"
        )

        if any(phrase in btn_text for phrase in SOLD_OUT_PHRASES):
            return {"status": "sold_out", "name": name, "price": price}

        for phrase in AVAILABLE_PHRASES:
            if phrase in btn_text and not disabled:
                return {"status": "available", "name": name, "price": price}

    # Fallback: check raw text for available phrases
    for phrase in AVAILABLE_PHRASES:
        if phrase in text:
            return {"status": "available", "name": name, "price": price}

    return {"status": "unknown", "name": name, "price": price}


def _fetch_with_requests(url: str) -> Optional[str]:
    """Fetch page HTML via requests with residential proxy."""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "DNT": "1",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
    }
    proxy = _get_requests_proxy()
    try:
        resp = requests.get(
            url, headers=headers, timeout=15,
            allow_redirects=True, proxies=proxy,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url[:80]}: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# §3  PLAYWRIGHT CHECKER (with proxy + stealth)
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_with_playwright(url: str) -> Optional[str]:
    """
    Open URL in stealth Chromium with residential proxy.
    Returns page HTML after JS rendering.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning("Playwright not installed — skipping browser check")
        return None

    # Import stealth (graceful fallback)
    stealth_patcher = None
    try:
        from playwright_stealth import Stealth
        stealth_patcher = Stealth()
    except ImportError:
        pass

    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)
    proxy = _get_playwright_proxy()

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

        ctx_kwargs = {
            "user_agent":  ua,
            "viewport":    vp,
            "locale":      "en-IN",
            "timezone_id": "Asia/Kolkata",
            "extra_http_headers": {
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        }
        if proxy:
            ctx_kwargs["proxy"] = proxy

        context = await browser.new_context(**ctx_kwargs)

        # Apply stealth
        if stealth_patcher:
            await stealth_patcher.apply_stealth(context)
        else:
            await context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                try{delete navigator.__proto__.webdriver}catch(e){}
                Object.defineProperty(navigator,'plugins',{
                    get:()=>[{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',
                    description:'Portable Document Format',length:1},
                    {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                    description:'',length:1}]
                });
                Object.defineProperty(navigator,'languages',{
                    get:()=>['en-IN','en-US','en','hi']
                });
                window.chrome={runtime:{connect:()=>{},sendMessage:()=>{}},
                    loadTimes:()=>({}),csi:()=>({})};
            """)

        page = await context.new_page()

        try:
            # Small random delay before navigation
            await asyncio.sleep(random.uniform(0.5, 2.0))

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except PWTimeout:
                pass

            # Quick scroll to trigger lazy-loaded content
            try:
                height = await page.evaluate("document.body.scrollHeight")
                for i in range(1, 4):
                    target_y = int((height / 3) * i)
                    await page.evaluate(f"window.scrollTo(0, {target_y})")
                    await asyncio.sleep(random.uniform(0.2, 0.5))
            except Exception:
                pass

            await asyncio.sleep(random.uniform(0.3, 0.8))
            html = await page.content()
            return html

        except PWTimeout:
            logger.warning(f"Playwright timeout on {url[:80]}")
            return None
        except Exception as e:
            logger.warning(f"Playwright error on {url[:80]}: {e}")
            return None
        finally:
            await context.close()
            await browser.close()


# ═════════════════════════════════════════════════════════════════════════════
# §4  PUBLIC ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def check_url_availability(url: str, use_browser: bool = True) -> dict:
    """
    Check ticket availability for a URL.

    Strategy:
      1. BMS API (instant, no browser) — if URL is BookMyShow
      2. Playwright + stealth + proxy — for JS-rendered pages
      3. Requests + proxy — lightweight fallback

    Retries up to 2 times with exponential back-off.
    """
    is_bms = "bookmyshow.com" in url.lower()

    # ── Strategy 1: BMS API (fastest, most reliable) ─────────────────────
    if is_bms:
        try:
            result = _check_bms_api(url)
            if result and result["status"] != "unknown":
                logger.info(f"BMS API check: {result['status']} for {url[:60]}")
                return result
        except Exception as e:
            logger.debug(f"BMS API check failed: {e}")

    # ── Strategy 2 & 3: Browser / Requests with retries ──────────────────
    max_retries = 2
    backoff = 3.0

    for attempt in range(max_retries + 1):
        try:
            html = None

            # Try Playwright with stealth + proxy
            if use_browser:
                try:
                    html = asyncio.run(_fetch_with_playwright(url))
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    html = loop.run_until_complete(_fetch_with_playwright(url))
                    loop.close()

            # Fallback to requests with proxy
            if html is None:
                html = _fetch_with_requests(url)

            if html is None:
                raise ValueError("All fetch methods returned no content")

            result = _parse_html(html, url)

            # Log what we found for debugging
            logger.info(
                f"Scraper: {result['status']} for {url[:60]} "
                f"(name={result.get('name', '')[:40]})"
            )
            return result

        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {url[:60]}: {e}")
            if attempt < max_retries:
                sleep_time = backoff * (2 ** attempt) + random.uniform(0, 2)
                logger.info(f"Retrying in {sleep_time:.1f}s")
                time.sleep(sleep_time)
            else:
                logger.error(f"All {max_retries + 1} attempts failed for {url[:60]}")
                return {
                    "status": "error", "name": "", "price": "",
                    "error": str(e),
                }
