import { toLatLon } from './solve.js'

const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
const TILE_ATTRIBUTION = '&copy; OpenStreetMap'
const TILE_CACHE = 'base-scanner-tiles-v1'
const PREFETCH_ZOOMS = [15, 16, 17]
const PREFETCH_RADIUS_M = 2000
const PREFETCH_MAX_TILES = 400
const PREFETCH_DELAY_MS = 80
const METERS_PER_DEGREE = 111320

function levelColour(value, low, high) {
  const span = high - low || 1
  const hue = 120 - 120 * Math.min(1, Math.max(0, (value - low) / span))
  return `hsl(${hue}, 85%, 52%)`
}

function tileX(lon, zoom) {
  return Math.floor(((lon + 180) / 360) * 2 ** zoom)
}

function tileY(lat, zoom) {
  const radians = (lat * Math.PI) / 180
  return Math.floor(((1 - Math.log(Math.tan(radians) + 1 / Math.cos(radians)) / Math.PI) / 2) * 2 ** zoom)
}

function tileUrlsAround(centre, radiusM) {
  const dLat = radiusM / METERS_PER_DEGREE
  const dLon = radiusM / (METERS_PER_DEGREE * Math.cos((centre.lat * Math.PI) / 180))
  const urls = []
  for (const zoom of PREFETCH_ZOOMS) {
    const xFrom = tileX(centre.lon - dLon, zoom)
    const xTo = tileX(centre.lon + dLon, zoom)
    const yFrom = tileY(centre.lat + dLat, zoom)
    const yTo = tileY(centre.lat - dLat, zoom)
    for (let x = xFrom; x <= xTo; x += 1) {
      for (let y = yFrom; y <= yTo; y += 1) {
        urls.push(TILE_URL.replace('{z}', zoom).replace('{x}', x).replace('{y}', y))
      }
    }
  }
  return urls
}

export async function prefetchTiles(centre, onProgress) {
  const urls = tileUrlsAround(centre, PREFETCH_RADIUS_M)
  if (urls.length > PREFETCH_MAX_TILES) {
    throw new Error(`ต้องโหลด ${urls.length} รูป เกินโควตาที่ตั้งไว้ ${PREFETCH_MAX_TILES}`)
  }
  const cache = await caches.open(TILE_CACHE)
  let done = 0
  for (const url of urls) {
    if (!(await cache.match(url, { ignoreVary: true }))) {
      const response = await fetch(url, { mode: 'cors' })
      if (response.ok) await cache.put(url, response)
      await new Promise((resolve) => setTimeout(resolve, PREFETCH_DELAY_MS))
    }
    done += 1
    onProgress(done, urls.length)
  }
  return urls.length
}

function peakWeight(weights) {
  let peak = 0
  for (let index = 0; index < weights.length; index += 1) {
    if (weights[index] > peak) peak = weights[index]
  }
  return peak
}

function heatCanvas(solution) {
  const canvas = document.createElement('canvas')
  canvas.width = solution.size
  canvas.height = solution.size
  const context = canvas.getContext('2d')
  const image = context.createImageData(solution.size, solution.size)
  const peak = peakWeight(solution.weights)
  for (let iy = 0; iy < solution.size; iy += 1) {
    for (let ix = 0; ix < solution.size; ix += 1) {
      const strength = (solution.weights[iy * solution.size + ix] / peak) ** 0.35
      const pixel = ((solution.size - 1 - iy) * solution.size + ix) * 4
      image.data[pixel] = 255
      image.data[pixel + 1] = Math.round(220 * (1 - strength))
      image.data[pixel + 2] = Math.round(80 * (1 - strength))
      image.data[pixel + 3] = Math.round(215 * strength)
    }
  }
  context.putImageData(image, 0, 0)
  return canvas
}

function contourSegments(solution) {
  const { size, mask, cellM, originM, home } = solution
  const inside = (ix, iy) => ix >= 0 && iy >= 0 && ix < size && iy < size && mask[iy * size + ix] === 1
  const corner = (ix, iy) => {
    const { lat, lon } = toLatLon(home, originM + ix * cellM, originM + iy * cellM)
    return [lat, lon]
  }
  const segments = []
  for (let iy = 0; iy < size; iy += 1) {
    for (let ix = 0; ix < size; ix += 1) {
      if (!inside(ix, iy)) continue
      if (!inside(ix - 1, iy)) segments.push([corner(ix, iy), corner(ix, iy + 1)])
      if (!inside(ix + 1, iy)) segments.push([corner(ix + 1, iy), corner(ix + 1, iy + 1)])
      if (!inside(ix, iy - 1)) segments.push([corner(ix, iy), corner(ix + 1, iy)])
      if (!inside(ix, iy + 1)) segments.push([corner(ix, iy + 1), corner(ix + 1, iy + 1)])
    }
  }
  return segments
}

export class MapView {
  #map
  #container
  #trackLine
  #ownMarker
  #accuracyRing
  #recordLayer
  #suggestionLayer
  #heatLayer = null
  #contourLayer = null
  #track = []

  constructor(container) {
    this.#container = container
    this.#map = L.map(container, { zoomControl: true, attributionControl: true }).setView([13.7563, 100.5018], 16)
    L.tileLayer(TILE_URL, { maxZoom: 19, crossOrigin: 'anonymous', attribution: TILE_ATTRIBUTION }).addTo(this.#map)
    this.#trackLine = L.polyline([], { color: '#5ac8fa', weight: 3, opacity: 0.7 }).addTo(this.#map)
    this.#recordLayer = L.layerGroup().addTo(this.#map)
    this.#suggestionLayer = L.layerGroup().addTo(this.#map)
  }

  attachTo(screen) {
    if (this.#container.parentElement !== screen) screen.appendChild(this.#container)
    setTimeout(() => this.#map.invalidateSize(), 0)
  }

  centreOn(lat, lon) {
    this.#map.setView([lat, lon], Math.max(this.#map.getZoom(), 16))
  }

  updatePosition(lat, lon, accuracy) {
    if (!this.#ownMarker) {
      this.#ownMarker = L.circleMarker([lat, lon], { radius: 7, color: '#fff', fillColor: '#5ac8fa', fillOpacity: 1 })
      this.#ownMarker.addTo(this.#map)
      this.#accuracyRing = L.circle([lat, lon], { radius: accuracy, color: '#5ac8fa', weight: 1, fillOpacity: 0.08 })
      this.#accuracyRing.addTo(this.#map)
      this.#map.setView([lat, lon], 17)
    }
    this.#ownMarker.setLatLng([lat, lon])
    this.#accuracyRing.setLatLng([lat, lon]).setRadius(accuracy)
    this.#track.push([lat, lon])
    this.#trackLine.setLatLngs(this.#track)
  }

  renderRecords(points, refs, home) {
    this.#recordLayer.clearLayers()
    const levels = points.map((point) => (point.delta === null ? point.l10 : point.delta))
    const low = Math.min(...levels)
    const high = Math.max(...levels)

    points.forEach((point) => {
      const level = point.delta === null ? point.l10 : point.delta
      L.circleMarker([point.lat, point.lon], {
        radius: 10,
        color: point.anchored ? '#fff' : '#888',
        weight: point.anchored ? 2 : 1,
        fillColor: levelColour(level, low, high),
        fillOpacity: 0.9,
      })
        .bindPopup(this.#popupHtml(point))
        .addTo(this.#recordLayer)
    })

    refs.forEach((ref) => {
      L.circleMarker([ref.lat, ref.lon], { radius: 7, color: '#ffd60a', weight: 2, fillOpacity: 0 })
        .bindPopup(this.#popupHtml(ref))
        .addTo(this.#recordLayer)
    })

    if (home) {
      L.marker([home.lat, home.lon]).bindPopup('บ้าน').addTo(this.#recordLayer)
    }
  }

  renderSuggestions(list) {
    this.#suggestionLayer.clearLayers()
    list.forEach((item) => {
      L.circle([item.lat, item.lon], { radius: item.radiusM, color: '#30d158', weight: 2, fillOpacity: 0.08 })
        .bindPopup(item.name)
        .addTo(this.#suggestionLayer)
      L.marker([item.lat, item.lon], {
        icon: L.divIcon({ className: 'suggest-pin', html: String(item.rank), iconSize: [26, 26] }),
      })
        .bindPopup(item.name)
        .addTo(this.#suggestionLayer)
    })
  }

  renderSolution(solution) {
    this.clearSolution()
    if (!solution) return
    const bounds = [
      [solution.bounds.south, solution.bounds.west],
      [solution.bounds.north, solution.bounds.east],
    ]
    this.#heatLayer = L.imageOverlay(heatCanvas(solution).toDataURL(), bounds, { opacity: 0.75 }).addTo(this.#map)
    this.#contourLayer = L.polyline(contourSegments(solution), { color: '#fff', weight: 2, opacity: 0.9 })
    this.#contourLayer.addTo(this.#map)
  }

  clearSolution() {
    if (this.#heatLayer) this.#map.removeLayer(this.#heatLayer)
    if (this.#contourLayer) this.#map.removeLayer(this.#contourLayer)
    this.#heatLayer = null
    this.#contourLayer = null
  }

  #popupHtml(record) {
    const rows = [
      ['เวลา', new Date(record.t).toLocaleString('th-TH')],
      ['L10', `${record.l10.toFixed(1)} dB`],
      ['เฉลี่ย', `${record.mean.toFixed(1)} dB`],
      ['Δ', record.delta === null || record.delta === undefined ? 'ไม่มีตัวยึด' : `${record.delta.toFixed(1)} dB`],
      ['ลม', `${record.wind.toFixed(1)} dB`],
      ['accuracy', `${record.acc.toFixed(0)} ม.`],
    ]
    const body = rows.map(([label, value]) => `<tr><td>${label}</td><td>${value}</td></tr>`).join('')
    return `<b>${record.name}</b><table>${body}</table>`
  }
}
