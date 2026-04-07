/**
 * queuePoller.js
 * ─────────────────────────────────────────────────────────────────────────────
 * Injected directly into the browser page context via page.evaluate().
 * Runs inside the waiting-room page — zero IPC latency on activation.
 *
 * Strategy
 * ─────────
 * • Adaptive polling frequency driven by queue position (far → near → instant).
 * • Uses MessageChannel for sub-4ms scheduling (beats setTimeout's 4ms floor).
 * • On "Active": fires a custom DOM event AND calls back via postMessage so the
 *   Playwright host can react in the same event-loop tick.
 * • Keeps a high-resolution timeline (performance.now) for diagnostics.
 */

(function installQueuePoller(config) {

  // ── Configuration ────────────────────────────────────────────────────────
  const {
    pollUrl,          // URL to poll for queue status  (string | null)
    statusSelector,   // CSS selector whose text carries the status (string | null)
    activeKeyword,    // text / JSON field value that means "you're in" (default: "active")
    positionSelector, // CSS selector for queue position number (string | null)
    checkoutUrl,      // where to redirect once active
  } = config;

  const ACTIVE_KW = (activeKeyword || 'active').toLowerCase();

  // Interval ladder (ms) keyed by queue-position bucket
  const LADDER = [
    { maxPos: 1,    interval: 0    },   // instant via MessageChannel
    { maxPos: 5,    interval: 16   },   // ~1 animation frame
    { maxPos: 20,   interval: 50   },
    { maxPos: 100,  interval: 250  },
    { maxPos: 500,  interval: 1000 },
    { maxPos: Infinity, interval: 4000 },
  ];

  let _timer       = null;
  let _active      = false;
  let _lastPos     = Infinity;
  let _pollCount   = 0;
  let _startTime   = performance.now();

  // ── MessageChannel zero-delay scheduler ─────────────────────────────────
  // Bypasses the 4 ms minimum that browsers impose on setTimeout.
  function nextTick(fn) {
    const { port1, port2 } = new MessageChannel();
    port1.onmessage = () => fn();
    port2.postMessage(null);
  }

  // ── Queue-position reader ────────────────────────────────────────────────
  function readPosition() {
    if (!positionSelector) return _lastPos;
    const el = document.querySelector(positionSelector);
    if (!el) return _lastPos;
    const n = parseInt(el.textContent.replace(/\D/g, ''), 10);
    return Number.isFinite(n) ? n : _lastPos;
  }

  // ── Interval selector ────────────────────────────────────────────────────
  function intervalForPos(pos) {
    for (const rung of LADDER) {
      if (pos <= rung.maxPos) return rung.interval;
    }
    return 4000;
  }

  // ── Activation handler ───────────────────────────────────────────────────
  function activate(source) {
    if (_active) return;
    _active = true;
    clearTimeout(_timer);

    const elapsed = (performance.now() - _startTime).toFixed(2);
    console.info(
      `[QueuePoller] ACTIVE after ${_pollCount} polls / ${elapsed} ms (source: ${source})`
    );

    // Notify Playwright host immediately via postMessage (caught in Node layer)
    window.postMessage({ type: 'QUEUE_ACTIVE', checkoutUrl, elapsed, source }, '*');

    // Also fire a DOM event for any in-page listeners
    document.dispatchEvent(new CustomEvent('queueActive', {
      detail: { checkoutUrl, elapsed, source }
    }));

    // Hard redirect — location.replace avoids a history entry
    if (checkoutUrl) {
      nextTick(() => { window.location.replace(checkoutUrl); });
    }
  }

  // ── Status check via fetch ───────────────────────────────────────────────
  async function fetchStatus() {
    if (!pollUrl) return null;
    try {
      const res  = await fetch(pollUrl, { cache: 'no-store', credentials: 'include' });
      const text = await res.text();
      // Try JSON first, fall back to plain text
      try {
        const json = JSON.parse(text);
        // Walk the JSON tree looking for the active keyword
        const str = JSON.stringify(json).toLowerCase();
        return str.includes(ACTIVE_KW) ? 'active' : str;
      } catch {
        return text.toLowerCase();
      }
    } catch {
      return null;
    }
  }

  // ── DOM status reader ────────────────────────────────────────────────────
  function domStatus() {
    if (!statusSelector) return null;
    const el = document.querySelector(statusSelector);
    return el ? el.textContent.trim().toLowerCase() : null;
  }

  // ── Single poll cycle ────────────────────────────────────────────────────
  async function poll() {
    if (_active) return;
    _pollCount++;

    const pos     = readPosition();
    _lastPos      = pos;
    const interval = intervalForPos(pos);

    // 1. Check DOM status first (zero-latency)
    const dom = domStatus();
    if (dom && dom.includes(ACTIVE_KW)) { activate('dom'); return; }

    // 2. Check page URL — some queues redirect automatically
    if (window.location.href.toLowerCase().includes(ACTIVE_KW)) {
      activate('url'); return;
    }

    // 3. Fetch the status endpoint (if configured)
    if (pollUrl) {
      const api = await fetchStatus();
      if (api && api.includes(ACTIVE_KW)) { activate('api'); return; }
    }

    // Schedule next poll
    if (interval === 0) {
      nextTick(poll);
    } else {
      _timer = setTimeout(poll, interval);
    }
  }

  // ── Mutation observer (catches instant DOM updates with zero polling cost)
  const observer = new MutationObserver(() => {
    const dom = domStatus();
    if (dom && dom.includes(ACTIVE_KW)) { observer.disconnect(); activate('mutation'); }
  });
  if (statusSelector) {
    const target = document.querySelector(statusSelector);
    if (target) {
      observer.observe(target, { childList: true, characterData: true, subtree: true });
    } else {
      observer.observe(document.body, { childList: true, subtree: true });
    }
  }

  // ── Kick off
  poll();
  console.info('[QueuePoller] Installed — watching for:', ACTIVE_KW);

})( /* config injected by queueMonitor.js */ __POLLER_CONFIG__ );
