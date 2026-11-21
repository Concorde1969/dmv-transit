// DMV Transit's service worker (served at /sw.js): lets the site install as an app, and
// shows a clear "You're offline" page when there's no connection, instead of the
// browser's error. Everything else goes to the network as usual: live trips, the map and
// the assistant all need it, so nothing else is cached here.
const CACHE = "dmvt-v2";
const OFFLINE = "/static/offline.html";
const PRECACHE = [OFFLINE, "/static/brand/logo.png", "/static/brand/icon-192.png"];
const PRECACHED = new Set(PRECACHE);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("dmvt-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));
  } else if (PRECACHED.has(url.pathname)) {
    // The offline page and its images: fresh when online, from the cache when not.
    event.respondWith(fetch(request).catch(() => caches.match(request)));
  }
});
