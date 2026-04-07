"""
TicketAlert — Public Availability Notifier
Flask backend with Web Push notifications for BookMyShow & District
"""

import json
import os
import smtplib
import threading
import time
import logging
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

try:
    from .scraper import check_url_availability
except ImportError:
    from scraper import check_url_availability

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
CORS(app)

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
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM watchers ORDER BY data->>'added_at'")
                watchers = [r["data"] for r in cur.fetchall()]
                cur.execute("SELECT data FROM subscriptions")
                subs = [r["data"] for r in cur.fetchall()]
        return {"watchers": watchers, "subscriptions": subs}

    def save_data(data):
        with _get_conn() as conn:
            with conn.cursor() as cur:
                for w in data["watchers"]:
                    cur.execute(
                        "INSERT INTO watchers(id,data) VALUES(%s,%s) "
                        "ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data",
                        (w["id"], json.dumps(w))
                    )
                cur.execute("DELETE FROM subscriptions")
                for s in data["subscriptions"]:
                    cur.execute(
                        "INSERT INTO subscriptions(endpoint,data) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                        (s.get("endpoint",""), json.dumps(s))
                    )
            conn.commit()

    def delete_watcher_db(watcher_id):
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM watchers WHERE id=%s", (watcher_id,))
            conn.commit()

    _init_db()
    logger.info("Using PostgreSQL for storage")

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
        return True
    except WebPushException as e:
        logger.error(f"Push failed: {e}")
        if hasattr(e, "response") and e.response and e.response.status_code == 410:
            return "expired"
        return False
    except Exception as e:
        logger.error(f"Push error: {e}")
        return False


def notify_all(watcher, status):
    data = load_data()
    subs = data.get("subscriptions", [])
    target_url = watcher.get("checkout_url") or watcher["url"]

    if status == "available":
        payload = {
            "type": "AVAILABLE",
            "title": "🎫 TICKETS AVAILABLE!",
            "body": f"{watcher['name']} — Open checkout now and complete OTP manually.",
            "url": target_url,
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

    # ── Direct alerts (SMS + Email) ───────────────────────────────────────────
    threading.Thread(target=send_sms_alert,   args=(sms_msg,),            daemon=True).start()
    threading.Thread(target=send_email_alert, args=(email_sub, email_body), daemon=True).start()

    # ── Web push (browser notifications) ─────────────────────────────────────
    stale = []
    for sub in subs:
        result = send_push(sub, payload)
        if result == "expired":
            stale.append(sub)
    if stale:
        data["subscriptions"] = [s for s in subs if s not in stale]
        save_data(data)


_monitor_thread = None
_stop_event = threading.Event()
_data_lock = threading.Lock()

USE_BROWSER = os.environ.get("USE_BROWSER", "true").lower() == "true"
MIN_CHECK_INTERVAL_SECONDS = max(5, int(os.environ.get("MIN_CHECK_INTERVAL_SECONDS", "5")))
MONITOR_LOOP_SECONDS = max(2, int(os.environ.get("MONITOR_LOOP_SECONDS", "2")))


def apply_check_result(watcher, result):
    status = result["status"]
    prev_status = watcher.get("last_status")

    if result.get("name") and watcher.get("name") in ("", "Checking…", "Checkingâ€¦", None):
        watcher["name"] = result["name"]

    watcher.update({
        "last_status": status,
        "last_checked_ts": time.time(),
        "last_checked": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    })
    if result.get("price"):
        watcher["price"] = result["price"]

    # Alert on every transition into available/upcoming,
    # OR if available and no alert has been sent yet (e.g. after restart)
    alert_needed = (
        (status != prev_status and status in ("available", "upcoming"))
        or (status == "available" and not watcher.get("alerted_at"))
    )
    if alert_needed:
        notify_all(watcher, status)
    if status == "available" and not watcher.get("alerted_at"):
        watcher["alerted_at"] = datetime.now().isoformat()

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
    return jsonify(load_data()["watchers"])

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
            "paused": False,
            "done": False,
            "added_at": datetime.now().isoformat(),
        }
        data["watchers"].append(watcher)
        save_data(data)

    return jsonify(watcher), 201

@app.route("/api/watchers/<watcher_id>", methods=["DELETE"])
def delete_watcher(watcher_id):
    with _data_lock:
        data = load_data()
        data["watchers"] = [w for w in data["watchers"] if w["id"] != watcher_id]
        if DATABASE_URL:
            delete_watcher_db(watcher_id)
        else:
            save_data(data)
    return jsonify({"ok": True})

@app.route("/api/watchers/<watcher_id>/pause", methods=["POST"])
def toggle_pause(watcher_id):
    with _data_lock:
        data = load_data()
        for w in data["watchers"]:
            if w["id"] == watcher_id:
                w["paused"] = not w.get("paused", False)
                save_data(data)
                return jsonify({"paused": w["paused"]})
    return jsonify({"error": "Not found"}), 404

@app.route("/api/watchers/<watcher_id>/check-now", methods=["POST"])
def check_now(watcher_id):
    with _data_lock:
        data = load_data()
        watcher = next((w for w in data["watchers"] if w["id"] == watcher_id), None)
        if not watcher:
            return jsonify({"error": "Not found"}), 404
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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})

@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")

if __name__ == "__main__":
    start_monitor()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
