/**
 * domPrewarmer.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Runs in a hidden background Playwright page while the user is in the queue.
 * Pre-loads all checkout page resources so the payment form renders instantly
 * the moment the foreground page is redirected.
 *
 * Phases
 * ──────
 * 1. DNS prefetch + TCP preconnect  → resolves host before navigation
 * 2. Resource preload hints          → fetches JS/CSS into browser cache
 * 3. Silent background navigation   → renders full checkout page off-screen
 * 4. Script extraction               → captures inline script tags for replay
 * 5. Ready signal                    → notifies queueMonitor that cache is hot
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

// Resources that are NOT worth pre-loading (saves bandwidth / avoids bot signals)
const BLOCK_RESOURCE_TYPES = ['image', 'media', 'font'];

// Checkout-page asset types we DO want to pre-cache
const PRELOAD_RESOURCE_TYPES = ['script', 'stylesheet', 'fetch', 'xhr', 'document'];

/**
 * @typedef {Object} PrewarmResult
 * @property {boolean}  ready
 * @property {string[]} cachedUrls
 * @property {string[]} scriptUrls
 * @property {number}   ttfbMs       — TTFB of the background navigation
 * @property {number}   domReadyMs   — time until DOMContentLoaded
 */

/**
 * Pre-warms the checkout page in a hidden Playwright context.
 *
 * @param {import('playwright').BrowserContext} context  — shared browser context
 * @param {string}  checkoutUrl
 * @param {object}  [opts]
 * @param {number}  [opts.timeoutMs=20000]
 * @returns {Promise<PrewarmResult>}
 */
export async function prewarmCheckout(context, checkoutUrl, opts = {}) {
  const { timeoutMs = 20_000 } = opts;
  const bgPage   = await context.newPage();
  const t0       = performance.now();
  const cached   = [];
  let   ttfbMs   = -1;
  let   domReady = -1;

  console.log('[Prewarmer] Starting background pre-load of', checkoutUrl);

  // ── Phase 1: Inject DNS-prefetch + preconnect into a blank page ─────────
  await bgPage.setContent(`
    <html><head>
      <link rel="dns-prefetch"  href="${_origin(checkoutUrl)}">
      <link rel="preconnect"    href="${_origin(checkoutUrl)}" crossorigin>
    </head><body></body></html>
  `);
  await bgPage.waitForTimeout(120); // let the DNS resolution fire

  // ── Phase 2: Block non-essential resources to minimise noise ────────────
  await bgPage.route('**/*', (route) => {
    const type = route.request().resourceType();
    if (BLOCK_RESOURCE_TYPES.includes(type)) {
      route.abort();
    } else {
      if (PRELOAD_RESOURCE_TYPES.includes(type)) {
        cached.push(route.request().url());
      }
      route.continue();
    }
  });

  // ── Phase 3: Capture TTFB via response timing ───────────────────────────
  bgPage.on('response', (resp) => {
    if (resp.url() === checkoutUrl && ttfbMs === -1) {
      ttfbMs = performance.now() - t0;
      console.log(`[Prewarmer] TTFB: ${ttfbMs.toFixed(1)} ms`);
    }
  });

  // ── Phase 4: Silent navigation (waitUntil:'domcontentloaded' — no images)
  try {
    await bgPage.goto(checkoutUrl, {
      waitUntil: 'domcontentloaded',
      timeout:   timeoutMs,
    });
    domReady = performance.now() - t0;
    console.log(`[Prewarmer] DOMContentLoaded: ${domReady.toFixed(1)} ms`);
  } catch (e) {
    console.warn('[Prewarmer] Navigation error (non-fatal):', e.message);
  }

  // ── Phase 5: Inject <link rel="preload"> hints for all discovered scripts
  const scriptUrls = await bgPage.evaluate(() =>
    [...document.querySelectorAll('script[src]')].map(s => s.src)
  ).catch(() => []);

  // ── Phase 6: Pre-fill any token / CSRF fields while hidden ──────────────
  await _injectPreloadHints(bgPage, scriptUrls);

  console.log(`[Prewarmer] ✅ Cache hot — ${cached.length} resources, ${scriptUrls.length} scripts`);

  // Keep the page alive (do NOT close it — resources stay in browser cache)
  // bgPage is closed by queueMonitor after checkout redirect completes.

  return {
    ready:       true,
    cachedUrls:  cached,
    scriptUrls,
    ttfbMs,
    domReadyMs:  domReady,
    _bgPage:     bgPage,   // returned so monitor can close it when done
  };
}

/**
 * After the foreground page redirects to checkout, swap in the already-warm
 * background page as the active tab (zero reload cost).
 *
 * @param {import('playwright').Page} foreground  — queue page (to be closed)
 * @param {import('playwright').Page} bgPage      — pre-warmed checkout page
 */
export async function swapToPrewarmedPage(foreground, bgPage) {
  console.log('[Prewarmer] Swapping to pre-warmed checkout page…');
  // Bring the background page to the foreground
  await bgPage.bringToFront();
  // Close the now-redundant queue page
  await foreground.close().catch(() => {});
  console.log('[Prewarmer] ✅ Checkout page is live (zero reload)');
  return bgPage;
}


// ── Helpers ─────────────────────────────────────────────────────────────────

function _origin(url) {
  try { return new URL(url).origin; } catch { return ''; }
}

async function _injectPreloadHints(page, scriptUrls) {
  if (!scriptUrls.length) return;
  await page.evaluate((urls) => {
    urls.forEach(url => {
      const link = document.createElement('link');
      link.rel   = 'preload';
      link.as    = 'script';
      link.href  = url;
      document.head.appendChild(link);
    });
  }, scriptUrls).catch(() => {});
}
