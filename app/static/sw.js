// TeamBots service worker: makes the app installable and keeps the shell available offline.
// Network first, so a new version always wins; the cache is only the fallback when the server
// cannot be reached. Nothing under /api is ever cached: chats and secrets stay on the server.
const CACHE = 'teambots-shell-v1';
const SHELL = ['/', '/static/app.css', '/static/app.js', '/static/icon.svg', '/static/icon-192.png', '/static/manifest.webmanifest'];

self.addEventListener('install', e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate', e => e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  }).catch(async () => (await caches.match(req, { ignoreSearch: true })) || (req.mode === 'navigate' && await caches.match('/')) || Response.error()));
});
