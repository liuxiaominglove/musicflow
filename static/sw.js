// Service Worker for MusicFlow PWA
// Cache-first strategy for app shell with size limit

const CACHE_NAME = 'musicflow-v1';
const MAX_CACHE_ITEMS = 50;

const PRECACHE_URLS = [
  '/mobile',
  '/manifest.json'
];

function trimCache(cache) {
  return cache.keys().then(keys => {
    if (keys.length > MAX_CACHE_ITEMS) {
      return cache.delete(keys[0]).then(() => trimCache(cache));
    }
  });
}

function cachePut(request, response) {
  return caches.open(CACHE_NAME).then(cache => {
    return cache.put(request, response).then(() => trimCache(cache));
  });
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return Promise.allSettled(
        PRECACHE_URLS.map(url => cache.add(url).catch(() => {}))
      );
    })
  );
  // Don't skip waiting — let user close old tabs first
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))
      );
    })
  );
  // Don't auto claim — avoid breaking existing tabs
});

// Network-first for API, cache-first for static assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Don't cache API calls or music streaming
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/music/')) {
    return;
  }

  // Cache-first for static assets
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        const fetchPromise = fetch(event.request).then((networkResponse) => {
          if (networkResponse.ok) {
            cachePut(event.request, networkResponse.clone());
          }
          return networkResponse;
        }).catch(() => cachedResponse);
        return cachedResponse;
      }
      return fetch(event.request).then((networkResponse) => {
        if (networkResponse.ok) {
          cachePut(event.request, networkResponse.clone());
        }
        return networkResponse;
      });
    })
  );
});
