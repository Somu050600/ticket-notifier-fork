// TicketAlert Service Worker
// Handles Web Push notifications with alarm behaviour

const CACHE_NAME = "ticketalert-v3";
const PRECACHE_URLS = ["/"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).catch(() => {})
  );
});

self.addEventListener("activate", (event) => {
  // Purge old caches on activation
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    ).then(() => clients.claim())
  );
});

// ── Push notification handler ─────────────────────────────────────────────────
self.addEventListener("push", (event) => {
  if (!event.data) return;

  let payload;
  try {
    payload = event.data.json();
  } catch {
    payload = { title: "TicketAlert", body: event.data.text() };
  }

  const isAlarm    = payload.alarm === true || payload.type === "AVAILABLE";
  const isCartReady = payload.type === "CART_READY";

  // Build notification options
  const options = {
    body: payload.body || "Ticket status changed",
    icon: payload.icon || "/static/icon-192.png",
    badge: payload.badge || "/static/badge.png",
    tag: payload.tag || "ticketalert",
    data: {
      url: payload.url || "/",
      type: payload.type || "UNKNOWN",
      watcherId: payload.watcher_id || null,
    },
    requireInteraction: payload.requireInteraction || isAlarm || isCartReady,
    silent: false,
    vibrate: payload.vibrate || (isAlarm ? [300, 100, 300, 100, 300, 100, 600] : [200]),
    actions: isCartReady
      ? [
          { action: "pay",     title: "Complete Payment" },
          { action: "dismiss", title: "Dismiss" },
        ]
      : isAlarm
      ? [
          { action: "open",    title: "Open Page" },
          { action: "dismiss", title: "Dismiss" },
        ]
      : [{ action: "open", title: "View" }],
  };

  event.waitUntil(
    self.registration.showNotification(payload.title || "TicketAlert", options)
  );
});

// ── Notification click handler ────────────────────────────────────────────────
self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  if (event.action === "dismiss") return;

  const url = event.notification.data?.url || "/";
  const type = event.notification.data?.type || "";
  const watcherId = event.notification.data?.watcherId || "";

  // For CART_READY and AVAILABLE — always open the target URL in a new tab
  // These are external BookMyShow/District URLs that need their own tab
  const isExternal = url.startsWith("http") && !url.includes(self.location.hostname);
  const appUrl = watcherId
    ? `/?alert=${encodeURIComponent(type.toLowerCase())}&watcher=${encodeURIComponent(watcherId)}&url=${encodeURIComponent(url)}`
    : "/";
  const appOpenUrl = `${appUrl}${appUrl.includes("?") ? "&" : "?"}open=1`;

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      if (type === "AVAILABLE") {
        for (const client of clientList) {
          if (client.url.includes(self.location.hostname)) {
            if ("focus" in client) client.focus();
            client.postMessage({
              type: "TICKET_AVAILABLE",
              url,
              watcherId,
              notificationType: type,
            });
            return;
          }
        }
        return clients.openWindow(appUrl);
      }

      if (type === "CART_READY" || isExternal) {
        for (const client of clientList) {
          if (client.url.includes(self.location.hostname)) {
            client.postMessage({
              type: "TICKET_AVAILABLE",
              url,
              watcherId,
              notificationType: type,
            });
          }
        }
        return clients.openWindow(appOpenUrl);
      }

      // For internal URLs: focus existing TicketAlert tab if open
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          client.focus();
          client.postMessage({ type: "TICKET_AVAILABLE", url, watcherId, notificationType: type });
          return;
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(appUrl);
      }
    })
  );
});

// ── Background sync ─────────────────────────────────────────────────────────
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-watchers") {
    event.waitUntil(fetch("/api/stats").catch(() => {}));
  }
});
