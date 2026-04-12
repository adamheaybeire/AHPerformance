// AH Performance — Service Worker
// Enables offline access and "Add to Home Screen" as a real app

const CACHE_NAME = 'ah-performance-v10';
const ASSETS_TO_CACHE = [
  '/AH-Performance-App.html',
  '/AH-Programme-Builder.html',
  '/AH-Exercise-Library-Browser.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png'
];

// Install: cache core app files
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first for API, cache-first for static assets
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls: always go to network (data must be fresh)
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() => {
        return new Response('{"offline":true}', {
          headers: { 'Content-Type': 'application/json' }
        });
      })
    );
    return;
  }

  // HTML files: network-first (always get fresh version, fall back to cache offline)
  if (url.pathname.endsWith('.html') || url.pathname === '/') {
    event.respondWith(
      fetch(event.request).then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // Other static assets (icons, manifest): cache-first with background update
  event.respondWith(
    caches.match(event.request).then(cached => {
      const networkFetch = fetch(event.request).then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);

      return cached || networkFetch;
    })
  );
});
