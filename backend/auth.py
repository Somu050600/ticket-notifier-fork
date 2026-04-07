"""
auth.py — Google OAuth 2.0 + per-user session management.

Each Gmail account gets its own isolated watcher list and checkout sessions.
Session is stored in a signed Flask cookie (server-side secret required).

Env vars needed:
  GOOGLE_CLIENT_ID      — from Google Cloud Console
  GOOGLE_CLIENT_SECRET  — from Google Cloud Console
  SECRET_KEY            — random string for cookie signing (generate once)
  BASE_URL              — public URL of this app (e.g. https://web-production-6500c.up.railway.app)
"""

import os
import json
import logging
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint, redirect, request, session,
    url_for, jsonify, current_app
)

logger = logging.getLogger("ticketalert.auth")

auth_bp = Blueprint("auth", __name__)

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL             = os.environ.get("BASE_URL", "").rstrip("/")

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "openid email profile"


# ── Helpers ───────────────────────────────────────────────────────────────────

def current_user() -> dict | None:
    """Returns the logged-in user dict or None."""
    return session.get("user")


def require_login(f):
    """Decorator — returns 401 JSON if not logged in (for API routes)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "Login required", "login_url": "/auth/login"}), 401
        return f(*args, **kwargs)
    return decorated


def user_id() -> str | None:
    """Returns the current user's unique ID (Google sub / email)."""
    u = current_user()
    return u.get("email") if u else None


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/auth/login")
def login():
    """Redirect browser to Google OAuth consent screen."""
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "GOOGLE_CLIENT_ID not configured"}), 500

    redirect_uri = f"{BASE_URL}/auth/callback"
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         SCOPES,
        "access_type":   "online",
        "prompt":        "select_account",   # lets different Gmail accounts sign in
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@auth_bp.route("/auth/callback")
def callback():
    """Handle Google's redirect after user grants permission."""
    code  = request.args.get("code")
    error = request.args.get("error")

    if error or not code:
        logger.warning(f"OAuth error: {error}")
        return redirect("/?auth=error")

    redirect_uri = f"{BASE_URL}/auth/callback"

    # Exchange code for tokens
    token_resp = requests.post(GOOGLE_TOKEN_URL, data={
        "code":          code,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=10)

    if not token_resp.ok:
        logger.error(f"Token exchange failed: {token_resp.text}")
        return redirect("/?auth=error")

    tokens       = token_resp.json()
    access_token = tokens.get("access_token")

    # Fetch user info
    user_resp = requests.get(
        GOOGLE_USERINFO,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not user_resp.ok:
        return redirect("/?auth=error")

    info = user_resp.json()
    session["user"] = {
        "email":   info.get("email", ""),
        "name":    info.get("name", ""),
        "picture": info.get("picture", ""),
        "sub":     info.get("sub", ""),
    }
    session.permanent = True
    logger.info(f"Login: {session['user']['email']}")
    return redirect("/?auth=success")


@auth_bp.route("/auth/logout")
def logout():
    session.clear()
    return redirect("/")


@auth_bp.route("/auth/me")
def me():
    """Returns current user info (or null if not logged in)."""
    return jsonify(current_user())
