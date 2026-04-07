// TicketAlert Service Worker
// Handles Web Push notifications with alarm behaviour

const CACHE_NAME = "ticketalert-v1";
const ALARM_ASSETS = ["/", "/static/alarm.mp3"];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(["/"])).catch(() => {})
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(clients.claim());
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

  const isAlarm = payload.alarm === true || payload.type === "AVAILABLE";
  const isTest  = payload.type === "TEST";

  // Build notification options
  const options = {
    body: payload.body || "Ticket status changed",
    icon: payload.icon || "/static/icon-192.png",
    badge: payload.badge || "/static/badge.png",
    tag: payload.tag || "ticketalert",
    data: { url: payload.url || "/" },
    requireInteraction: payload.requireInteraction || isAlarm,
    silent: false,
    vibrate: payload.vibrate || (isAlarm ? [300, 100, 300, 100, 300, 100, 600] : [200]),
    actions: isAlarm
      ? [
          { action: "open",    title: "🎫 Open Page" },
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

  // Determine whether the target is external (e.g. BookMyShow checkout URL)
  // or internal (the TicketAlert app itself).
  const isExternal = url.startsWith("http") && !url.includes(self.location.hostname);

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      // For external URLs (checkout pages) always open a new tab so the user
      // lands directly on the booking page — never reuse the TicketAlert tab.
      if (isExternal) {
        // Also notify any open TicketAlert tab so the banner shows there too
        for (const client of clientList) {
          if (client.url.includes(self.location.hostname)) {
            client.postMessage({ type: "TICKET_AVAILABLE", url });
          }
        }
        return clients.openWindow(url);
      }

      // For internal URLs: focus existing TicketAlert tab if open
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          client.focus();
          client.postMessage({ type: "TICKET_AVAILABLE", url });
          return;
        }
      }
      // Otherwise open new tab
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});

// ── Background sync (re-check on network restore) ────────────────────────────
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-watchers") {
    event.waitUntil(fetch("/api/stats").catch(() => {}));
  }
});
