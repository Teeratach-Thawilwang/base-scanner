const APP_CACHE = 'base-scanner-app-v1'
const TILE_CACHE = 'base-scanner-tiles-v1'
const TILE_HOST = 'tile.openstreetmap.org'

const APP_SHELL = [
  './',
  './index.html',
  './app/audio.js',
  './app/main.js',
  './app/map.js',
  './app/measure.js',
  './app/solve.js',
  './app/store.js',
  './app/suggest.js',
  './vendor/leaflet/leaflet.js',
  './vendor/leaflet/leaflet.css',
  './vendor/leaflet/images/marker-icon.png',
  './vendor/leaflet/images/marker-icon-2x.png',
  './vendor/leaflet/images/marker-shadow.png',
  './vendor/leaflet/images/layers.png',
  './vendor/leaflet/images/layers-2x.png',
]

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(APP_CACHE).then((cache) => cache.addAll(APP_SHELL)))
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name !== APP_CACHE && name !== TILE_CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  )
})

async function serveTile(request) {
  const cache = await caches.open(TILE_CACHE)
  const hit = await cache.match(request, { ignoreVary: true })
  if (hit) return hit
  const response = await fetch(request)
  if (response.ok) cache.put(request, response.clone())
  return response
}

async function serveApp(request) {
  const cache = await caches.open(APP_CACHE)
  try {
    const response = await fetch(request)
    if (response.ok) cache.put(request, response.clone())
    return response
  } catch (error) {
    const hit = await cache.match(request, { ignoreSearch: true })
    if (hit) return hit
    throw error
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET') return
  if (url.hostname === TILE_HOST) {
    event.respondWith(serveTile(event.request))
    return
  }
  if (url.origin === self.location.origin) event.respondWith(serveApp(event.request))
})
