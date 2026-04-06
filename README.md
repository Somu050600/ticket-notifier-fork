# TicketAlert v2 — Availability Notifier

Alarm-style push notifications when tickets go live on BookMyShow, District, Insider, and more.

## What is new in v2

- Headless Chromium (Playwright) for JS-rendered pages
- Human-like scrolling + random delays between every action
- 14-UA rotation (desktop, mobile, Chrome, Firefox, Safari, Edge)
- Exponential back-off retry (up to 2x) on failure
- PostgreSQL for production storage (falls back to data.json locally)
- Thread-safe monitor loop
- Extra platforms: Insider, Paytm Events, Zomato Events, TicketNew, Kyazoonga

## Supported Platforms

- BookMyShow (bookmyshow.com / in.bookmyshow.com)
- District (district.in)
- Insider (insider.in)
- Paytm Events (paytm.com/event)
- Zomato Events (zomato.com/events)
- TicketNew (ticketnew.com)
- Kyazoonga (kyazoonga.com)

## Local Setup

    pip install -r requirements.txt
    playwright install chromium
    playwright install-deps chromium   # Linux only
    python generate_keys.py            # generates VAPID keys once

Copy the output into `.env`, then:

    python start.py
    # Visit http://localhost:5000

## Deploy to Railway (recommended)

`railway.toml` is configured for Railway and starts the background monitor loop through Gunicorn.

1. Push repo to GitHub
2. railway.app > New Project > Deploy from GitHub
3. Add a PostgreSQL service inside Railway
4. In the web service, set `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `CONTACT_EMAIL`
5. Set `USE_BROWSER=true`
6. Set `DATABASE_URL=${{Postgres.DATABASE_URL}}` or connect the shared variable from the PostgreSQL service
7. Deploy and confirm `GET /health` returns `{"status":"ok",...}`

## Deploy to Render

render.yaml provisions web service + Postgres database automatically.

1. Push repo to GitHub
2. render.com > New > Blueprint > connect repo
3. Render reads render.yaml — add VAPID keys + CONTACT_EMAIL
4. Click Apply

## Deploy to Heroku

    heroku create your-ticket-alert
    heroku addons:create heroku-postgresql:essential-0
    heroku config:set VAPID_PRIVATE_KEY="..." VAPID_PUBLIC_KEY="..." CONTACT_EMAIL="..." USE_BROWSER=true
    heroku buildpacks:add heroku/python
    heroku buildpacks:add https://github.com/mxschmitt/heroku-playwright-buildpack
    git push heroku main

## Environment Variables

    VAPID_PRIVATE_KEY   required  from generate_keys.py
    VAPID_PUBLIC_KEY    required  from generate_keys.py
    CONTACT_EMAIL       required  your email for VAPID claims
    DATABASE_URL        optional  PostgreSQL (auto-set on Railway/Render)
    USE_BROWSER         optional  true/false, default true
    PORT                optional  auto-set by host

## How the scraper works

1. Playwright opens Chromium with a random UA + viewport (desktop or mobile)
2. Navigates to URL, waits for network idle
3. Scrolls down in 3-6 steps with 0.3-0.9s pauses between each
4. Extracts fully-rendered HTML, parses for availability keywords
5. Falls back to plain requests if Playwright unavailable
6. Retries 2x with exponential back-off on any failure
