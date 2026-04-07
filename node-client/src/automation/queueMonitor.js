/**
 * queueMonitor.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Orchestrates the full "Waiting Room → Active Checkout" pipeline:
 *
 *  ┌──────────────────────────────────────────────────────────┐
 *  │  1. Launch lean Chromium (minimal args, no extras)       │
 *  │  2. Open queue page  +  inject high-precision poller     │
 *  │  3. Simultaneously pre-warm checkout in hidden BG page   │
 *  │  4. Poller fires "ACTIVE" → swap BG page to foreground   │
 *  │  5. Hand off to checkout.js automation                   │
 *  └──────────────────────────────────────────────────────────┘
 *
 * Usage:
 *   QUEUE_URL=https://...  CHECKOUT_URL=https://... node src/automation/queueMonitor.js
 *
 * Optional env vars:
 *   QUEUE_POLL_URL       API endpoint that returns queue status JSON
 *   QUEUE_STATUS_SEL     CSS selector whose text carries the status
 *   QUEUE_POSITION_SEL   CSS selector for the position number
 *   QUEUE_ACTIVE_KW      Keyword meaning "you're through" (default: "active")
 */

import { chromium }         from 'playwright';
import { readFileSync }     from 'fs';
import { fileURLToPath }    from 'url';
import path                 from 'path';
import { prewarmCheckout, swapToPrewarmedPage } from './domPrewarmer.js';
import { reserveListing }   from '../checkout.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Environment ──────────────────────────────────────────────────────────────
const QUEUE_URL         = process.env.QUEUE_URL        ?? '';
const CHECKOUT_URL      = process.env.CHECKOUT_URL     ?? process.env.EVENT_URL ?? '';
const POLL_URL          = process.env.QUEUE_POLL_URL   ?? null;
const STATUS_SEL        = process.env.QUEUE_STATUS_SEL    ?? null;
const POSITION_SEL      = process.env.QUEUE_POSITION_SEL  ?? null;
const ACTIVE_KW         = process.env.QUEUE_ACTIVE_KW     ?? 'active';

if (!QUEUE_URL || !CHECKOUT_URL) {
  console.error('[QueueMonitor] QUEUE_URL and CHECKOUT_URL are required.');
  process.exit(1);
}

// ── Lean browser launch args (minimise TTFB / startup overhead) ──────────────
const LEAN_ARGS = [
  '--no-sandbox',
  '--disable-blink-features=AutomationControlled',
  '--disable-dev-shm-usage',
  '--disable-gpu',
  '--disable-extensions',
  '--disable-background-networking',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',        // keeps hidden tabs at full speed
  '--disable-features=TranslateUI',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-default-apps',
  '--metrics-recording-only',
  '--mute-audio',
];

// ── Load the poller source (injected as a string into the page) ──────────────
const POLLER_SRC = readFileSync(
  path.join(__dirname, 'queuePoller.js'), 'utf8'
);

/**
 * Builds the self-invoking poller script with the config baked in.
 */
function buildPollerScript(config) {
  return POLLER_SRC.replace(
    '/* config injected by queueMonitor.js */ __POLLER_CONFIG__',
    JSON.stringify(config)
  );
}

// ── Timing utilities ─────────────────────────────────────────────────────────
const t0 = performance.now();
const elapsed = () => `${(performance.now() - t0).toFixed(1)} ms`;

// ── Main ─────────────────────────────────────────────────────────────────────
async function main() {
  console.log('[QueueMonitor] Launching lean Chromium…');

  const browser = await chromium.launch({
    headless: true,
    args:     LEAN_ARGS,
  });

  const context = await browser.newContext({
    userAgent: (
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
      + 'AppleWebKit/537.36 (KHTML, like Gecko) '
      + 'Chrome/124.0.0.0 Safari/537.36'
    ),
    viewport:      { width: 1280, height: 800 },
    locale:        'en-IN',
    timezoneId:    'Asia/Kolkata',
    javaScriptEnabled: true,
  });

  // Mask automation fingerprints
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
    window.chrome = { runtime: {} };
  });

  // ── Open queue page ──────────────────────────────────────────────────────
  const queuePage = await context.newPage();
  console.log(`[QueueMonitor] Opening queue: ${QUEUE_URL}`);
  await queuePage.goto(QUEUE_URL, { waitUntil: 'domcontentloaded', timeout: 30_000 });

  // ── Start pre-warming checkout in background (parallel, non-blocking) ────
  console.log('[QueueMonitor] Pre-warming checkout in background…');
  const prewarmPromise = prewarmCheckout(context, CHECKOUT_URL, { timeoutMs: 25_000 })
    .catch(e => {
      console.warn('[QueueMonitor] Pre-warm failed (non-fatal):', e.message);
      return { ready: false, _bgPage: null };
    });

  // ── Inject high-precision poller into the queue page ────────────────────
  const pollerConfig = {
    pollUrl:          POLL_URL,
    statusSelector:   STATUS_SEL,
    activeKeyword:    ACTIVE_KW,
    positionSelector: POSITION_SEL,
    checkoutUrl:      CHECKOUT_URL,
  };

  await queuePage.addScriptTag({
    content: buildPollerScript(pollerConfig),
  });

  console.log(`[QueueMonitor] Poller injected at ${elapsed()} — waiting for ACTIVE signal…`);

  // ── Wait for activation (whichever fires first) ──────────────────────────
  const activationSignal = new Promise((resolve) => {

    // 1. postMessage from in-page poller
    queuePage.on('console', (msg) => {
      if (msg.text().includes('[QueuePoller] ACTIVE')) resolve({ source: 'console' });
    });

    // 2. Playwright-visible navigation (page redirects itself)
    queuePage.on('framenavigated', (frame) => {
      if (frame === queuePage.mainFrame()) {
        const url = frame.url().toLowerCase();
        if (url.includes(ACTIVE_KW) || url === CHECKOUT_URL.toLowerCase()) {
          resolve({ source: 'navigation', url: frame.url() });
        }
      }
    });

    // 3. Evaluate bridge — polls postMessage events from inside the page
    queuePage.exposeFunction('__queueActive__', (detail) => {
      resolve({ source: 'bridge', detail });
    });

    queuePage.evaluate(() => {
      window.addEventListener('message', (e) => {
        if (e.data?.type === 'QUEUE_ACTIVE') {
          window.__queueActive__(e.data);
        }
      });
    });
  });

  // ── Block until activation ───────────────────────────────────────────────
  const signal   = await activationSignal;
  const prewarm  = await prewarmPromise;

  console.log(
    `[QueueMonitor] ✅ ACTIVE at ${elapsed()} `
    + `(source: ${signal.source}, pre-warm: ${prewarm.ready ? 'ready' : 'missed'})`
  );

  // ── Swap to pre-warmed page (or navigate if pre-warm missed) ────────────
  let checkoutPage;
  if (prewarm.ready && prewarm._bgPage) {
    checkoutPage = await swapToPrewarmedPage(queuePage, prewarm._bgPage);
  } else {
    // Pre-warm wasn't ready — navigate the queue page directly (fastest alternative)
    console.log('[QueueMonitor] Navigating queue page to checkout (pre-warm unavailable)…');
    await queuePage.goto(CHECKOUT_URL, { waitUntil: 'commit' }); // 'commit' = first byte
    checkoutPage = queuePage;
  }

  console.log(`[QueueMonitor] Checkout page live at ${elapsed()}`);

  // ── Performance summary ──────────────────────────────────────────────────
  const metrics = await checkoutPage.evaluate(() => {
    const nav = performance.getEntriesByType('navigation')[0];
    return nav ? {
      ttfb:          nav.responseStart - nav.requestStart,
      domInteractive: nav.domInteractive,
      domComplete:    nav.domComplete,
    } : null;
  }).catch(() => null);

  if (metrics) {
    console.log(
      `[QueueMonitor] 📊 Checkout TTFB: ${metrics.ttfb?.toFixed(1)} ms | `
      + `DOM interactive: ${metrics.domInteractive?.toFixed(1)} ms`
    );
  }

  // ── Hand off to checkout automation ─────────────────────────────────────
  console.log('[QueueMonitor] Handing off to checkout automation…');
  await reserveListing(
    { listingId: 'auto', quantity: 1, eventId: 'auto' },
    (payload) => {
      console.log('[QueueMonitor] 🎫 Hold secured:', JSON.stringify(payload, null, 2));
    }
  );

  await browser.close();
}

main().catch((err) => {
  console.error('[QueueMonitor] Fatal:', err.message);
  process.exit(1);
});
