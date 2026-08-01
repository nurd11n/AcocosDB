/* ACOCOS /pos/ service worker — app shell only.
 *
 * Bump SHELL_VERSION on every deploy that changes CSS/JS/fonts/icons so the
 * old shell cache is dropped on activate and nobody is stuck on stale CSS.
 * This file is served by apps.pos.views.service_worker at /pos/sw.js (not
 * under /static/) so its default scope covers all of /pos/.
 */
const SHELL_VERSION = "28";
const CACHE_NAME = `acocos-pos-shell-v${SHELL_VERSION}`;

// App shell only: CSS, JS, fonts, icons. Never HTML, never data — stale
// stock or totals would mean a real oversell, so pages are always fetched
// fresh from the network (see the fetch handler below).
const SHELL_URLS = [
  "/static/pos/tokens.css",
  "/static/pos/app.css",
  "/static/pos/fonts/InterVariable.woff2",
  "/static/pos/js/confirm.js",
  "/static/pos/js/pos.js",
  "/static/pos/icons/icon-192.png",
  "/static/pos/icons/icon-512.png",
  "/static/pos/icons/icon-512-maskable.png",
  "/static/vendor/htmx/htmx.min.js",
];

// This service worker must never intercept these — /panel/ and /login/ need
// always-fresh auth state, /wa/ is the WhatsApp webhook, not a browser page.
const NEVER_INTERCEPT_PREFIXES = ["/panel/", "/login/", "/wa/"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith("acocos-pos-shell-") && name !== CACHE_NAME)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (NEVER_INTERCEPT_PREFIXES.some((p) => url.pathname.startsWith(p))) {
    return; // let the browser handle it — no interception at all
  }
  if (event.request.method !== "GET") {
    return; // sales, payments, cancels — always go straight to the network
  }
  if (!SHELL_URLS.includes(url.pathname)) {
    return; // HTML + data: network-first with no offline fallback, untouched
  }

  // App shell: cache-first so repeat visits load instantly, refilling the
  // cache from the network on a miss (e.g. a first visit, or a new file
  // added to SHELL_URLS after a deploy that bumped SHELL_VERSION).
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
    })
  );
});
