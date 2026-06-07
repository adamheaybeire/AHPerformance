// AH Performance — Service Worker
// Enables offline access and "Add to Home Screen" as a real app

const CACHE_NAME = 'ah-performance-v114';
const ASSETS_TO_CACHE = [
  '/AH-Performance-App.html',
  '/AH-Programme-Builder.html',
  '/AH-Exercise-Library-Browser.html',
  '/AH-Hyrox-Class-Builder.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/favicon.ico'
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

  // Photos: cache-first (images don't change once uploaded)
  if (url.pathname.startsWith('/api/photos/')) {
    event.respondWith(
      caches.match(event.request).then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

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

// ─── Push Notifications ───
// Fires when server sends a push message (even if app is closed)
self.addEventListener('push', event => {
  let data = { title: 'AH Performance', body: 'You have a new update', icon: '/icon-192.png', badge: '/icon-192.png', url: '/AH-Performance-App.html' };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch(e) {}

  const options = {
    body: data.body,
    icon: data.icon || '/icon-192.png',
    badge: data.badge || '/icon-192.png',
    tag: data.tag || 'ah-notification',
    renotify: true,
    vibrate: [200, 100, 200],
    data: { url: data.url || '/AH-Performance-App.html' }
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// When user taps the notification, open/focus the app
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/AH-Performance-App.html';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // If app is already open, focus it
      for (const client of windowClients) {
        if (client.url.includes('AH-Performance-App') && 'focus' in client) {
          return client.focus();
        }
      }
      // Otherwise open a new window
      return clients.openWindow(url);
    })
  );
});
