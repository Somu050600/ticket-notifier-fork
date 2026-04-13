"""
TicketAlert — Public Availability Notifier
Flask backend with Web Push notifications for BookMyShow & District
"""

import collections
import json
import os
import re
import smtplib
import threading
import time
import logging
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory, session
from flask_cors import CORS
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

try:
    from .scraper import check_url_availability
    from .autocheckout import (trigger_auto_checkout, claim_slot,
                                get_session, get_session_for_device, inject_otp,
                                start_worker)
    from .auth import auth_bp, current_user, require_login, user_id
except ImportError:
    from scraper import check_url_availability
    from autocheckout import (trigger_auto_checkout, claim_slot,
                               get_session, get_session_for_device, inject_otp,
                               start_worker)
    from auth import auth_bp, current_user, require_login, user_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("ticketalert")

app = Flask(
    __name__,
    template_folder=str(ROOT_DIR / "frontend" / "templates"),
    static_folder=str(ROOT_DIR / "frontend" / "static"),
)
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    import secrets
    _secret = secrets.token_hex(32)
    logger.warning("SECRET_KEY not set — generated ephemeral key (sessions won't survive restarts)")
app.secret_key = _secret

# ── Session cookie config (critical for mobile browsers) ─────────────────────
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 30   # 30 days
# Set Secure=True only when running behind HTTPS (Railway always uses HTTPS)
if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("BASE_URL", "").startswith("https"):
    app.config["SESSION_COOKIE_SECURE"] = True

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── CORS — restrict origins in production ────────────────────────────────────
_base_url = os.environ.get("BASE_URL", "").rstrip("/")
_cors_origins = [_base_url] if _base_url else ["*"]
CORS(app, supports_credentials=True, origins=_cors_origins)
app.register_blueprint(auth_bp)

# ── Simple in-memory rate limiter ────────────────────────────────────────────
_rate_buckets: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()

def _check_rate_limit(key: str, max_requests: int = 30, window_seconds: int = 60) -> bool:
    """Returns True if rate limit exceeded."""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, collections.deque())
        # Purge old entries
        while bucket and bucket[0] < now - window_seconds:
            bucket.popleft()
        if len(bucket) >= max_requests:
            return True
        bucket.append(now)
    return False

@app.before_request
def _rate_limit_check():
    """Apply rate limiting to API endpoints."""
    if request.path.startswith("/api/"):
        # Rate limit by IP + user session
        ip = request.remote_addr or "unknown"
        uid = user_id() or ip
        key = f"{uid}:{request.path}"
        if _check_rate_limit(key, max_requests=60, window_seconds=60):
            return jsonify({"error": "Too many requests — slow down"}), 429

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": f"mailto:{os.environ.get('CONTACT_EMAIL', 'alerts@ticketalert.app')}"}

# ── Direct alert config ───────────────────────────────────────────────────────
ALERT_PHONE  = os.environ.get("ALERT_PHONE",  "+918368272979")   # Twilio SMS target
ALERT_EMAIL  = os.environ.get("ALERT_EMAIL",  "rahulgulati712@gmail.com")

# Twilio (set these in Railway env vars)
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN",  "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM_NUMBER", "")   # e.g. +1XXXXXXXXXX

# SMTP / Gmail (set these in Railway env vars)
SMTP_HOST    = os.environ.get("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ.get("SMTP_USER",     "")        # your Gmail address
SMTP_PASS    = os.environ.get("SMTP_PASS",     "")        # Gmail App Password


def send_sms_alert(message: str):
    """Send an SMS via Twilio to ALERT_PHONE."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        logger.warning("Twilio credentials not set — skipping SMS alert")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=ALERT_PHONE)
        logger.info(f"SMS sent to {ALERT_PHONE}")
    except Exception as e:
        logger.error(f"SMS failed: {e}")


def send_ring_call(event_name: str, cart_url: str = ""):
    """
    Ring ALERT_PHONE with a Twilio voice call.
    The call speaks the event name and cart URL so the user knows
    exactly what's ready — even if they're away from the screen.
    Repeats the message twice to make sure they hear it.
    """
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        logger.warning("Twilio credentials not set — skipping ring call")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)

        # TwiML spoken when the user picks up
        # Strip all XML-unsafe chars and limit length to prevent TwiML injection
        safe_name = re.sub(r'[<>&"\']', '', event_name)[:120]
        twiml = f"""
        <Response>
            <Say voice="alice" language="en-IN" loop="2">
                Ticket Alert! Your cart is ready for {safe_name}.
                Open Ticket Alert on your phone or browser and tap the pay now button immediately.
                Your cart will expire in a few minutes. Act now!
            </Say>
            <Pause length="1"/>
            <Say voice="alice" language="en-IN">
                Repeating: Cart is ready for {safe_name}. Open Ticket Alert and pay now. Goodbye.
            </Say>
        </Response>
        """.strip()

        call = client.calls.create(
            to=ALERT_PHONE,
            from_=TWILIO_FROM,
            twiml=twiml,
            timeout=30,          # ring for 30 seconds max
        )
        logger.info(f"Ring call initiated to {ALERT_PHONE} — SID: {call.sid}")
    except Exception as e:
        logger.error(f"Ring call failed: {e}")


def send_email_alert(subject: str, body: str):
    """Send an email via SMTP to ALERT_EMAIL."""
    if not all([SMTP_USER, SMTP_PASS]):
        logger.warning("SMTP credentials not set — skipping email alert")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = ALERT_EMAIL
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_EMAIL, msg.as_string())
        logger.info(f"Email sent to {ALERT_EMAIL}")
    except Exception as e:
        logger.error(f"Email failed: {e}")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    def _get_conn():
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

    def _init_db():
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS watchers (
                        id TEXT PRIMARY KEY,
                        data JSONB NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        endpoint TEXT PRIMARY KEY,
                        data JSONB NOT NULL
                    );
                """)
            conn.commit()
        logger.info("PostgreSQL tables ready")

    def load_data():
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM watchers ORDER BY data->>'added_at'")
                    watchers = [r["data"] for r in cur.fetchall()]
                    cur.execute("SELECT data FROM subscriptions")
                    subs = [r["data"] for r in cur.fetchall()]
            return {"watchers": watchers, "subscriptions": subs}
        except Exception as e:
            logger.error(f"load_data failed: {e}")
            return {"watchers": [], "subscriptions": []}

    def save_data(data):
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    for w in data["watchers"]:
                        cur.execute(
                            "INSERT INTO watchers(id,data) VALUES(%s,%s) "
                            "ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data",
                            (w["id"], json.dumps(w))
                        )
                    # Upsert subscriptions instead of delete-all + reinsert
                    # to avoid losing data during concurrent requests
                    for s in data["subscriptions"]:
                        endpoint = s.get("endpoint", "")
                        if endpoint:
                            cur.execute(
                                "INSERT INTO subscriptions(endpoint,data) VALUES(%s,%s) "
                                "ON CONFLICT(endpoint) DO UPDATE SET data=EXCLUDED.data",
                                (endpoint, json.dumps(s))
                            )
                conn.commit()
        except Exception as e:
            logger.error(f"save_data failed: {e}")
            raise

    def delete_watcher_db(watcher_id):
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM watchers WHERE id=%s", (watcher_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"delete_watcher_db failed: {e}")

    # Retry DB init up to 3 times (Railway PG can take a moment to become ready)
    for _attempt in range(3):
        try:
            _init_db()
            logger.info("Using PostgreSQL for storage")
            break
        except Exception as e:
            logger.warning(f"DB init attempt {_attempt + 1} failed: {e}")
            if _attempt == 2:
                logger.error("Could not connect to PostgreSQL after 3 attempts")
                raise
            time.sleep(2)

else:
    DATA_FILE = ROOT_DIR / "data.json"

    def load_data():
        if DATA_FILE.exists():
            return json.loads(DATA_FILE.read_text())
        return {"watchers": [], "subscriptions": []}

    def save_data(data):
        DATA_FILE.write_text(json.dumps(data, indent=2))

    def delete_watcher_db(watcher_id):
        pass

    logger.info("Using JSON file for storage")


def send_push(subscription_info, payload):
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS,
        )
        logger.info(f"Web push sent to endpoint: {subscription_info.get('endpoint', '')[:30]}...")
        return True
    except WebPushException as e:
        logger.error(f"Push failed: {e}")
        if hasattr(e, "response") and e.response and e.response.status_code == 410:
            return "expired"
        return False
    except Exception as e:
        logger.error(f"Push error: {e}")
        return False


def _derive_checkout_url(event_url: str) -> str:
    """
    Auto-generate a BookMyShow buytickets URL from an event URL.
    /sports/event-name/ET001234  →  /buytickets/event-name/ET001234
    This is the seat selection entry point (qty popup → stadium map).
    Falls back to the original URL if not a recognizable pattern.
    """
    # BookMyShow: .../sports/slug/ETXXXXXX or .../events/slug/ETXXXXXX
    m = re.search(r'in\.bookmyshow\.com/(?:sports|events)/([^?#]+)', event_url)
    if m:
        slug = m.group(1).rstrip('/')
        return f"https://in.bookmyshow.com/buytickets/{slug}"
    # Already a buytickets URL — keep it
    if 'buytickets' in event_url:
        return event_url
    # District.in — keep as-is
    if 'district.in' in event_url:
        return event_url
    return event_url


def notify_all(watcher, status):
    with _data_lock:
        data = load_data()
    subs = list(data.get("subscriptions", []))  # defensive copy
    target_url = (
        watcher.get("cart_url")
        or watcher.get("checkout_url")
        or _derive_checkout_url(watcher["url"])
    )

    if status == "available":
        payload = {
            "type": "AVAILABLE",
            "title": "🎫 TICKETS AVAILABLE!",
            "body": f"{watcher['name']} — Open checkout now and complete OTP manually.",
            "url": target_url,
            "watcher_id": watcher["id"],
            "alarm": True,
            "vibrate": [200, 100, 200, 100, 200, 100, 400],
            "requireInteraction": True,
            "tag": f"available-{watcher['id']}",
        }
        sms_msg   = f"🎫 TICKETS AVAILABLE: {watcher['name']}\nBook now: {target_url}"
        email_sub = f"🎫 Tickets Available — {watcher['name']}"
        email_body = (
            f"Tickets are NOW AVAILABLE for:\n\n"
            f"  {watcher['name']}\n\n"
            f"Book here: {target_url}\n\n"
            f"— TicketAlert"
        )
    elif status == "upcoming":
        payload = {
            "type": "UPCOMING",
            "title": "⏰ Sale Opening Soon!",
            "body": f"{watcher['name']} — Ticket sale is about to begin!",
            "url": target_url,
            "watcher_id": watcher["id"],
            "alarm": False,
            "tag": f"upcoming-{watcher['id']}",
        }
        sms_msg   = f"⏰ Sale opening soon: {watcher['name']}\n{target_url}"
        email_sub = f"⏰ Sale Opening Soon — {watcher['name']}"
        email_body = (
            f"Ticket sale is about to begin for:\n\n"
            f"  {watcher['name']}\n\n"
            f"Link: {target_url}\n\n"
            f"— TicketAlert"
        )
    else:
        return

    # ── Auto-checkout (non-blocking — enqueues to background worker) ─────────
    if status == "available":
        checkout_url = watcher.get("checkout_url") or _derive_checkout_url(watcher["url"])
        trigger_auto_checkout(
            watcher["id"], checkout_url,
            cart_mode=watcher.get("cart_mode", True),
            target_price=watcher.get("target_price", ""),
            owner_email=watcher.get("owner", ""),
        )

    # ── Direct alerts (SMS + Email + Ring Call) ────────────────────────────────
    threading.Thread(target=send_sms_alert,   args=(sms_msg,),            daemon=True).start()
    threading.Thread(target=send_email_alert, args=(email_sub, email_body), daemon=True).start()
    # Ring call only for "available" — the most time-critical moment
    if status == "available":
        threading.Thread(target=send_ring_call,
                         args=(watcher['name'], target_url),
                         daemon=True).start()

    # ── Web push (browser notifications) ─────────────────────────────────────
    stale_endpoints = []
    owner = watcher.get("owner", "")
    for sub in subs:
        if not owner or sub.get("owner") == owner:
            result = send_push(sub, payload)
            if result == "expired":
                stale_endpoints.append(sub.get("endpoint"))
    if stale_endpoints:
        with _data_lock:
            fresh_data = load_data()
            fresh_data["subscriptions"] = [
                s for s in fresh_data["subscriptions"]
                if s.get("endpoint") not in stale_endpoints
            ]
            save_data(fresh_data)


_monitor_thread = None
_stop_event = threading.Event()
_data_lock = threading.Lock()

USE_BROWSER = os.environ.get("USE_BROWSER", "true").lower() == "true"
MIN_CHECK_INTERVAL_SECONDS = max(3, int(os.environ.get("MIN_CHECK_INTERVAL_SECONDS", "5")))
MONITOR_LOOP_SECONDS = max(2, int(os.environ.get("MONITOR_LOOP_SECONDS", "2")))


def apply_check_result(watcher, result):
    status = result["status"]
    prev_status = watcher.get("last_status")

    if result.get("name") and watcher.get("name") in ("", "Checking\u2026", "Checking...", None):
        watcher["name"] = result["name"]

    watcher.update({
        "last_status": status,
        "last_checked_ts": time.time(),
        "last_checked": datetime.now().isoformat(),
    })
    if result.get("price"):
        watcher["price"] = result["price"]

    # Alert once when transitioning into "available" or "upcoming".
    # Set alerted_at BEFORE calling notify_all to prevent double-fire
    # on rapid consecutive checks.
    alert_needed = (
        (status != prev_status and status in ("available", "upcoming"))
        or (status == "available" and not watcher.get("alerted_at"))
    )
    if alert_needed:
        if status == "available":
            watcher["alerted_at"] = datetime.now().isoformat()
        notify_all(watcher, status)

    return watcher


def monitor_loop():
    logger.info(
        "Monitor loop started (browser=%s, min_interval=%ss, tick=%ss)",
        "yes" if USE_BROWSER else "no",
        MIN_CHECK_INTERVAL_SECONDS,
        MONITOR_LOOP_SECONDS,
    )
    while not _stop_event.is_set():
        try:
            with _data_lock:
                data = load_data()
            changed = False

            for watcher in data["watchers"]:
                if watcher.get("paused") or watcher.get("done"):
                    continue
                interval = max(MIN_CHECK_INTERVAL_SECONDS, watcher.get("interval_seconds", MIN_CHECK_INTERVAL_SECONDS))
                if time.time() - watcher.get("last_checked_ts", 0) < interval:
                    continue

                logger.info(f"Checking: {watcher['name']} ({watcher['url']})")
                result = check_url_availability(watcher["url"], use_browser=USE_BROWSER)
                apply_check_result(watcher, result)
                changed = True

            if changed:
                with _data_lock:
                    save_data(data)

        except Exception as e:
            logger.error(f"Monitor loop error: {e}")

        time.sleep(MONITOR_LOOP_SECONDS)


def start_monitor():
    global _monitor_thread
    start_worker()
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _stop_event.clear()
    _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    _monitor_thread.start()


@app.route("/")
def index():
    return render_template("index.html", vapid_public_key=VAPID_PUBLIC_KEY)

@app.route("/api/vapid-public-key")
def vapid_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})

@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    sub_info = request.json
    sub_info["owner"] = user_id()   # tag subscription with logged-in Gmail
    with _data_lock:
        data = load_data()
        if not any(s.get("endpoint") == sub_info.get("endpoint") for s in data["subscriptions"]):
            data["subscriptions"].append(sub_info)
            save_data(data)
    return jsonify({"ok": True, "count": len(data["subscriptions"])})

@app.route("/api/unsubscribe", methods=["POST"])
def unsubscribe():
    endpoint = request.json.get("endpoint")
    with _data_lock:
        data = load_data()
        data["subscriptions"] = [s for s in data["subscriptions"] if s.get("endpoint") != endpoint]
        save_data(data)
    return jsonify({"ok": True})

@app.route("/api/watchers", methods=["GET"])
def get_watchers():
    uid = user_id()
    all_w = load_data()["watchers"]
    # If logged in, show only this user's watchers; else show legacy (no owner) watchers
    if uid:
        return jsonify([w for w in all_w if w.get("owner") == uid])
    return jsonify([w for w in all_w if not w.get("owner")])

ALLOWED_DOMAINS = [
    "bookmyshow.com", "in.bookmyshow.com", "district.in",
    "insider.in", "paytm.com/event", "zomato.com/events",
    "ticketnew.com", "kyazoonga.com",
]

@app.route("/api/watchers", methods=["POST"])
def add_watcher():
    body = request.json
    url = body.get("url", "").strip()
    checkout_url = body.get("checkout_url", "").strip()
    name = body.get("name", "").strip()
    try:
        interval = int(body.get("interval_seconds", MIN_CHECK_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return jsonify({"error": "Check interval must be a number"}), 400
    interval = max(5, interval)

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not any(d in url for d in ALLOWED_DOMAINS):
        return jsonify({"error": f"Supported: {', '.join(ALLOWED_DOMAINS)}"}), 400
    if checkout_url and not any(d in checkout_url for d in ALLOWED_DOMAINS):
        return jsonify({"error": "Checkout URL must be on a supported ticketing site"}), 400

    with _data_lock:
        data = load_data()
        if any(w["url"] == url for w in data["watchers"]):
            return jsonify({"error": "Already watching this URL"}), 400

        platform = "bookmyshow"
        for d in ["district.in", "insider.in", "paytm.com", "zomato.com", "ticketnew.com", "kyazoonga.com"]:
            if d in url:
                platform = d.split(".")[0]
                break

        target_price = body.get("target_price", "").strip()   # e.g. "1500" or "₹1500"
        cart_mode    = bool(body.get("cart_mode", True))       # default: add-to-cart only

        watcher = {
            "id": str(uuid.uuid4())[:8],
            "url": url,
            "checkout_url": checkout_url,
            "name": name or "Checking…",
            "platform": platform,
            "interval_seconds": interval,
            "last_status": None,
            "last_checked": None,
            "last_checked_ts": 0,
            "price": "",
            "target_price": target_price,
            "cart_mode": cart_mode,
            "cart_url": None,
            "paused": False,
            "done": False,
            "added_at": datetime.now().isoformat(),
            "owner": user_id(),          # tied to logged-in Gmail account
        }
        data["watchers"].append(watcher)
        save_data(data)

    return jsonify(watcher), 201

def _validate_watcher_id(watcher_id: str) -> bool:
    """Sanity-check watcher_id format to prevent injection."""
    return bool(re.match(r'^[a-f0-9\-]{4,40}$', watcher_id))

def _owns_watcher(watcher: dict) -> bool:
    """Check that the current user owns a watcher (or it's legacy unowned)."""
    uid = user_id()
    owner = watcher.get("owner", "")
    if uid and owner and uid != owner:
        return False
    return True

@app.route("/api/watchers/<watcher_id>", methods=["DELETE"])
def delete_watcher(watcher_id):
    if not _validate_watcher_id(watcher_id):
        return jsonify({"error": "Invalid watcher ID"}), 400
    with _data_lock:
        data = load_data()
        watcher = next((w for w in data["watchers"] if w["id"] == watcher_id), None)
        if not watcher:
            return jsonify({"error": "Not found"}), 404
        if not _owns_watcher(watcher):
            return jsonify({"error": "Not authorized"}), 403
        data["watchers"] = [w for w in data["watchers"] if w["id"] != watcher_id]
        if DATABASE_URL:
            delete_watcher_db(watcher_id)
        else:
            save_data(data)
    return jsonify({"ok": True})

@app.route("/api/watchers/<watcher_id>/pause", methods=["POST"])
def toggle_pause(watcher_id):
    if not _validate_watcher_id(watcher_id):
        return jsonify({"error": "Invalid watcher ID"}), 400
    with _data_lock:
        data = load_data()
        for w in data["watchers"]:
            if w["id"] == watcher_id:
                if not _owns_watcher(w):
                    return jsonify({"error": "Not authorized"}), 403
                w["paused"] = not w.get("paused", False)
                save_data(data)
                return jsonify({"paused": w["paused"]})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/watchers/<watcher_id>/check-now", methods=["POST"])
def check_now(watcher_id):
    if not _validate_watcher_id(watcher_id):
        return jsonify({"error": "Invalid watcher ID"}), 400
    with _data_lock:
        data = load_data()
        watcher = next((w for w in data["watchers"] if w["id"] == watcher_id), None)
        if not watcher:
            return jsonify({"error": "Not found"}), 404
        if not _owns_watcher(watcher):
            return jsonify({"error": "Not authorized"}), 403
        url = watcher["url"]

    result = check_url_availability(url, use_browser=USE_BROWSER)

    with _data_lock:
        data = load_data()
        watcher = next((w for w in data["watchers"] if w["id"] == watcher_id), None)
        if not watcher:
            return jsonify({"error": "Not found"}), 404
        apply_check_result(watcher, result)
        save_data(data)
        return jsonify(watcher)

@app.route("/api/test-notification", methods=["POST"])
def test_notification():
    sub_info = request.json.get("subscription")
    if not sub_info:
        return jsonify({"error": "No subscription provided"}), 400
    payload = {
        "type": "TEST",
        "title": "🔔 TicketAlert Test — ALARM!",
        "body": "Push notifications are working perfectly!",
        "url": "/",
        "alarm": True,
        "vibrate": [200, 100, 200, 100, 200, 100, 400],
        "requireInteraction": True,
        "tag": "test-alarm",
    }
    result = send_push(sub_info, payload)
    return jsonify({"ok": result is True})

@app.route("/api/stats")
def stats():
    data = load_data()
    w = data["watchers"]
    return jsonify({
        "total": len(w),
        "active": sum(1 for x in w if not x.get("paused") and not x.get("done")),
        "available": sum(1 for x in w if x.get("last_status") == "available"),
        "sold_out": sum(1 for x in w if x.get("last_status") == "sold_out"),
        "subscribers": len(data["subscriptions"]),
    })

@app.route("/api/watchers/<watcher_id>/cart-url", methods=["POST"])
def update_cart_url(watcher_id):
    """Internal — called by autocheckout to store the captured cart URL."""
    cart_url = (request.json or {}).get("cart_url", "")

    # Validate: reject bare homepage URLs and derive proper booking URL
    from urllib.parse import urlparse
    if cart_url:
        parsed = urlparse(cart_url)
        if not parsed.path or parsed.path.rstrip('/') == '':
            logger.warning(f"cart-url for {watcher_id} was homepage ({cart_url}) — deriving proper URL")
            cart_url = ""  # will be replaced below

    with _data_lock:
        data = load_data()
        for w in data["watchers"]:
            if w["id"] == watcher_id:
                # If cart_url is empty/invalid, derive from the watcher's event URL
                if not cart_url:
                    cart_url = _derive_checkout_url(w.get("checkout_url") or w.get("url", ""))
                    logger.info(f"Derived cart URL for {watcher_id}: {cart_url}")
                w["cart_url"] = cart_url
                save_data(data)
                # Push cart-ready notification to the watcher's owner
                _send_cart_notification(w, cart_url)
                return jsonify({"ok": True})
    return jsonify({"error": "Not found"}), 404


def _send_cart_notification(watcher, cart_url):
    """Push + SMS + email to the watcher owner when their cart is ready."""
    data = load_data()
    payload = {
        "type":              "CART_READY",
        "title":             "TICKETS LIVE - Book Now!",
        "body":              f"{watcher['name']} - Tap to open booking page and grab your seats!",
        "url":               cart_url,
        "watcher_id":        watcher["id"],
        "alarm":             True,
        "requireInteraction": True,
        "vibrate":           [300, 100, 300, 100, 600],
        "tag":               f"cart-{watcher['id']}",
    }
    owner = watcher.get("owner", "")
    for sub in data.get("subscriptions", []):
        if not owner or sub.get("owner") == owner:
            send_push(sub, payload)

    sms = f"🛒 Cart ready for {watcher['name']}! Pay here: {cart_url}"
    threading.Thread(target=send_sms_alert,   args=(sms,),  daemon=True).start()
    threading.Thread(target=send_email_alert,
                     args=(f"🛒 Cart Ready — {watcher['name']}", sms),
                     daemon=True).start()
    # RING the user's phone — this is the most urgent alert
    threading.Thread(target=send_ring_call,
                     args=(watcher['name'], cart_url),
                     daemon=True).start()


@app.route("/health")
def health():
    db_ok = True
    if DATABASE_URL:
        try:
            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception:
            db_ok = False
    monitor_ok = _monitor_thread is not None and _monitor_thread.is_alive()
    status = "ok" if (db_ok and monitor_ok) else "degraded"
    return jsonify({
        "status": status,
        "ts": datetime.now().isoformat(),
        "database": "connected" if db_ok else "error",
        "monitor": "running" if monitor_ok else "stopped",
    }), 200 if status == "ok" else 503

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("index.html", vapid_public_key=VAPID_PUBLIC_KEY), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal error: {e}")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Internal server error"}), 500
    return render_template("index.html", vapid_public_key=VAPID_PUBLIC_KEY), 500

@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")

@app.route("/api/claim-slot", methods=["POST"])
def api_claim_slot():
    """
    A device calls this to claim the next available checkout slot (card).
    Returns { session_id, card_priority } or { session_id: null } if none free.
    """
    body       = request.json or {}
    watcher_id = body.get("watcher_id", "").strip()
    device_id  = body.get("device_id",  "").strip()
    if not watcher_id or not device_id:
        return jsonify({"error": "watcher_id and device_id are required"}), 400
    session_id = claim_slot(watcher_id, device_id)
    if session_id:
        sess = get_session(session_id)
        return jsonify({"session_id": session_id,
                        "card_priority": sess.get("card_priority", 0)})
    return jsonify({"session_id": None})


@app.route("/api/checkout-status/<session_id>")
def checkout_status(session_id):
    """Frontend polls this with its session_id to track checkout progress."""
    return jsonify(get_session(session_id))


@app.route("/api/submit-otp", methods=["POST"])
def submit_otp():
    """Receives OTP from the user and delivers it to the correct session."""
    body       = request.json or {}
    session_id = body.get("session_id", "").strip()
    otp        = body.get("otp",        "").strip()
    if not session_id or not otp:
        return jsonify({"error": "session_id and otp are required"}), 400
    inject_otp(session_id, otp)
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_monitor()   # start_monitor() calls start_worker() internally
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
