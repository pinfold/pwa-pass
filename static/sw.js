const CACHE_NAME = 'opencommons-v2'; // Increment this (v1 -> v2) to force an update
const urlsToCache = [
  '/',
  '/manifest.json'
];

// 1. INSTALL: Force the new service worker to take over immediately
self.addEventListener('install', event => {
  self.skipWaiting(); 
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(urlsToCache);
    })
  );
});

// 2. ACTIVATE: Delete old caches so the phone doesn't get confused
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cache => {
          if (cache !== CACHE_NAME) {
            console.log('Service Worker: Clearing Old Cache');
            return caches.delete(cache);
          }
        })
      );
    })
  );
});

// 3. FETCH: Network-First Strategy
// This tries the internet first. If offline, it uses the cache.
// This ensures you always see the "New Code" while developing.
self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => {
      return caches.match(event.request);
    })
  );
});
