import { AudioMeter } from './audio.js'
import { MapView, prefetchTiles } from './map.js'
import { anchorPoints, referenceDrift, startBeacon, startMeasurement } from './measure.js'
import { solve, thresholds } from './solve.js'
import { Store, downloadCsv, parseCsv, toCsv } from './store.js'
import { suggestPoints } from './suggest.js'

const GPS_MAX_ACCURACY_M = 30
const SPECTRUM_FLOOR_DB = -110
const SPECTRUM_CEILING_DB = -20
const SPECTRUM_MARKS_HZ = [45, 70]
const STALL_WINDOW_POINTS = 3
const COMPASS = ['เหนือ', 'ตะวันออกเฉียงเหนือ', 'ตะวันออก', 'ตะวันออกเฉียงใต้', 'ใต้', 'ตะวันตกเฉียงใต้', 'ตะวันตก', 'ตะวันตกเฉียงเหนือ']

const el = (id) => document.getElementById(id)

const meter = new AudioMeter()
const store = new Store()
const state = {
  position: null,
  pending: null,
  solution: null,
  suggestions: [],
  beaconStop: null,
  beaconAt: null,
  running: false,
  wakeLock: null,
  bestTrail: [],
}

let mapView = null

function compassName(x, y) {
  const degrees = (Math.atan2(x, y) * 180) / Math.PI
  const index = Math.round(((degrees + 360) % 360) / 45) % COMPASS.length
  return COMPASS[index]
}

function stamp() {
  return new Date().toISOString().slice(0, 16).replace(/[-:]/g, '').replace('T', '-')
}

function setScreen(name) {
  document.querySelectorAll('.screen').forEach((screen) => screen.classList.toggle('active', screen.id === `screen-${name}`))
  document.querySelectorAll('nav.tabs button').forEach((tab) => tab.classList.toggle('active', tab.dataset.screen === name))
  if (name === 'map') {
    mapView.attachTo(el('slot-map'))
    mapView.clearSolution()
  }
  if (name === 'result') {
    mapView.attachTo(el('slot-result'))
    mapView.renderSolution(state.solution)
  }
}

function showBanner(kind, text) {
  const banner = el('banner')
  banner.className = `banner show ${kind}`
  banner.textContent = text
}

function hideBanner() {
  el('banner').className = 'banner'
}

function gpsReady() {
  return state.position !== null && state.position.acc <= GPS_MAX_ACCURACY_M
}

function firstProblem() {
  const processing = meter.running ? meter.activeProcessing() : []
  if (state.pending && state.pending.windy) {
    return ['bad', `ลมแรง ค่าไม่น่าเชื่อถือ สัญญาณสูงกว่าย่านลมแค่ ${state.pending.windMargin.toFixed(1)} dB บันทึกไม่ได้`]
  }
  if (!gpsReady()) return ['warn', 'GPS ยังไม่นิ่ง รอให้ค่า accuracy ต่ำกว่า 30 ม. ก่อนบันทึก']
  if (processing.length > 0) return ['warn', `มือถือยังเปิด ${processing.join(', ')} อยู่ ค่าที่วัดได้จะถูกปรับระดับอัตโนมัติ`]
  if (store.refs.length === 0) return ['warn', 'ยังไม่มีจุดอ้างอิง วัดที่จุดอ้างอิงแล้วกดตั้งจุดอ้างอิงก่อน']
  if (referenceDrift(store.refs) > 3) {
    return ['warn', `ค่าจุดอ้างอิงเลื่อนไปแล้ว ${referenceDrift(store.refs).toFixed(1)} dB ต้นตอกำลังดังขึ้นเบาลง กลับไปวัดจุดอ้างอิงถี่ขึ้น`]
  }
  return null
}

function renderStatus() {
  el('micState').textContent = meter.running ? `ไมค์ ${Math.round(meter.sampleRate / 1000)} kHz` : 'ไมค์ยังไม่เปิด'
  el('micState').className = `chip ${meter.running ? 'good' : ''}`
  if (state.position) {
    el('gpsState').textContent = `GPS ±${state.position.acc.toFixed(0)} ม.`
    el('gpsState').className = `chip ${gpsReady() ? 'good' : 'bad'}`
  }
  el('countState').textContent = `${store.points.length} จุด / อ้างอิง ${store.refs.length}`

  const problem = firstProblem()
  if (problem) showBanner(problem[0], problem[1])
  else hideBanner()

  const busy = state.running || state.beaconStop !== null
  el('btnMeasure').disabled = busy
  el('btnSavePoint').disabled = busy || !state.pending || state.pending.windy || !gpsReady()
  el('btnSetRef').disabled = busy || !state.pending || state.pending.windy || !gpsReady()
  el('btnBeacon').disabled = state.running || (!state.beaconStop && !gpsReady())
  el('btnBeacon').classList.toggle('on', state.beaconStop !== null)
  el('btnBeacon').textContent = state.beaconStop ? 'ปิดโหมด beacon' : 'เปิดโหมด beacon'
}

function deltaOf(result) {
  const anchored = anchorPoints([{ ...result, t: result.startedAt }], store.refs)[0]
  return anchored.anchored ? anchored.delta : null
}

function renderReadout() {
  if (!state.pending) return
  const delta = deltaOf(state.pending)
  el('l10Value').textContent = state.pending.l10.toFixed(1)
  el('meanValue').textContent = state.pending.mean.toFixed(1)
  el('windValue').textContent = state.pending.windMargin.toFixed(1)
  if (delta === null) {
    el('deltaValue').textContent = '--.-'
    el('deltaHint').textContent = 'ยังไม่มีจุดอ้างอิงที่ใกล้พอ ค่านี้เทียบกับอะไรไม่ได้'
    el('deltaValue').style.color = 'var(--muted)'
    return
  }
  el('deltaValue').textContent = `${delta > 0 ? '↑' : '↓'}${Math.abs(delta).toFixed(1)}`
  el('deltaValue').style.color = delta > 3 ? 'var(--bad)' : delta < -3 ? 'var(--good)' : 'var(--text)'
  el('deltaHint').textContent = Math.abs(delta) < 3 ? 'ต่างน้อยกว่า 3 dB กลางแจ้งถือว่าเท่าเดิม' : 'ต่างเกิน 3 dB เชื่อถือได้'
}

function levelSpread(anchored) {
  const usable = anchored.filter((point) => point.anchored)
  if (usable.length < 2) return null
  const deltas = usable.map((point) => point.delta)
  return Math.max(...deltas) - Math.min(...deltas)
}

function trackBest(solution) {
  const last = state.bestTrail[state.bestTrail.length - 1]
  if (last && last.pointCount === solution.pointCount) return
  state.bestTrail.push({ pointCount: solution.pointCount, x: solution.best.x, y: solution.best.y })
}

function bestStalledM(solution) {
  const earlier = state.bestTrail.find((entry) => entry.pointCount === solution.pointCount - STALL_WINDOW_POINTS)
  if (!earlier) return null
  return Math.sqrt((earlier.x - solution.best.x) ** 2 + (earlier.y - solution.best.y) ** 2)
}

function stopVerdict(solution) {
  if (solution.areaKm2 < thresholds.STOP_AREA_KM2) return 'พื้นที่แคบพอแล้ว หยุดสำรวจได้ เดินเข้าไปฟังด้วยหูต่อ'
  const moved = bestStalledM(solution)
  if (moved !== null && moved < thresholds.STOP_MOVE_M) {
    return `วัดเพิ่ม ${STALL_WINDOW_POINTS} จุดแล้วจุดที่น่าจะเป็นต้นตอขยับแค่ ${moved.toFixed(0)} ม. หยุดสำรวจได้`
  }
  return 'ยังไม่ถึงเกณฑ์หยุด วัดเพิ่มตามจุดที่แอปแนะนำ'
}

function renderSummary(anchored) {
  const usable = anchored.filter((point) => point.anchored).length
  const spread = levelSpread(anchored)
  const lines = [
    '<h2>สถานะการสำรวจ</h2>',
    `วัดไปแล้ว ${anchored.length} จุด ใช้คำนวณได้ ${usable} จุด จุดอ้างอิง ${store.refs.length} ครั้ง`,
  ]

  if (spread !== null) {
    const verdict = spread < thresholds.OUTDOOR_MEANINGFUL_DB ? 'น้อยเกินไป ขยายรัศมีวงเป็นเท่าตัว อย่าเดินถี่ขึ้น' : 'ใช้ได้'
    lines.push(`<br />ผลต่างดังสุดกับเงียบสุด ${spread.toFixed(1)} dB ${verdict}`)
  }

  if (!state.solution) {
    lines.push(`<br />ต้องมีจุดที่มีตัวยึดอย่างน้อย ${thresholds.MIN_POINTS} จุดก่อนจึงจะคำนวณได้`)
    el('resultSummary').innerHTML = lines.join('')
    return
  }

  const best = state.solution.best
  lines.push(
    '<h2>ตำแหน่งที่น่าจะเป็นต้นตอ</h2>',
    `<div class="big">${best.lat.toFixed(5)}, ${best.lon.toFixed(5)}</div>`,
    `ห่างบ้าน ${best.distanceFromHomeM.toFixed(0)} ม. ทาง${compassName(best.x, best.y)}`,
    `<br />พื้นที่ในเส้นขอบ 90% ${state.solution.areaKm2.toFixed(3)} ตร.กม.`,
    `<br />รูปแบบการแผ่ที่เข้ากับข้อมูลที่สุด n = ${best.exponent}`,
    `<br />${stopVerdict(state.solution)}`,
  )
  el('resultSummary').innerHTML = lines.join('')
}

function renderData() {
  const anchored = anchorPoints(store.points, store.refs)
  const home = store.home
  state.solution = home ? solve(anchored, home) : null
  if (state.solution) trackBest(state.solution)
  state.suggestions = home
    ? suggestPoints({
        solution: state.solution,
        home,
        reference: store.refs[store.refs.length - 1] || null,
        standing: state.position,
      })
    : []

  mapView.renderRecords(anchored, store.refs, home)
  mapView.renderSuggestions(state.suggestions)
  if (el('screen-result').classList.contains('active')) mapView.renderSolution(state.solution)
  renderSummary(anchored)
  renderStatus()
}

function sizeSpectrum() {
  const canvas = el('spectrum')
  const ratio = window.devicePixelRatio || 1
  canvas.width = canvas.clientWidth * ratio
  canvas.height = canvas.clientHeight * ratio
}

function drawSpectrum(spectrum) {
  const canvas = el('spectrum')
  const context = canvas.getContext('2d')
  const { width, height } = canvas
  const [lowHz, highHz] = meter.signalBandHz
  context.clearRect(0, 0, width, height)

  for (let bin = 0; bin < spectrum.length; bin += 1) {
    const hz = bin * meter.binWidthHz
    const level = (spectrum[bin] - SPECTRUM_FLOOR_DB) / (SPECTRUM_CEILING_DB - SPECTRUM_FLOOR_DB)
    const barHeight = Math.max(0, Math.min(1, level)) * height
    context.fillStyle = hz >= lowHz && hz <= highHz ? '#5ac8fa' : '#39414a'
    context.fillRect((bin / spectrum.length) * width, height - barHeight, width / spectrum.length + 1, barHeight)
  }

  context.strokeStyle = '#ffd60a'
  SPECTRUM_MARKS_HZ.forEach((hz) => {
    const x = (hz / meter.spectrumTopHz) * width
    context.beginPath()
    context.moveTo(x, 0)
    context.lineTo(x, height)
    context.stroke()
  })
}

async function keepScreenAwake() {
  if (!('wakeLock' in navigator) || state.wakeLock) return
  try {
    state.wakeLock = await navigator.wakeLock.request('screen')
    state.wakeLock.addEventListener('release', () => {
      state.wakeLock = null
    })
  } catch {
    state.wakeLock = null
  }
}

async function measureOnce() {
  state.running = true
  renderStatus()
  await keepScreenAwake()
  const run = startMeasurement(meter, {
    onFrame: (frame, remainingMs) => {
      drawSpectrum(frame.spectrum)
      el('btnMeasure').textContent = `กำลังวัด ${Math.ceil(remainingMs / 1000)} วิ`
    },
  })
  try {
    state.pending = await run.done
  } finally {
    state.running = false
    el('btnMeasure').textContent = 'วัด 30 วิ'
  }
  renderReadout()
  renderStatus()
}

function recordFrom(result, name) {
  return {
    t: result.startedAt,
    lat: state.position.lat,
    lon: state.position.lon,
    acc: state.position.acc,
    name,
    l10: result.l10,
    mean: result.mean,
    wind: result.wind,
  }
}

function savePending(kind) {
  const fallback = kind === 'ref' ? `อ้างอิง ${store.refs.length + 1}` : `จุด ${store.points.length + 1}`
  store.addRecord(kind, recordFrom(state.pending, el('pointName').value.trim() || fallback))
  el('pointName').value = ''
  state.pending = null
  el('deltaHint').textContent = 'บันทึกแล้ว'
  renderData()
}

function toggleBeacon() {
  if (state.beaconStop) {
    state.beaconStop()
    state.beaconStop = null
    renderStatus()
    return
  }
  if (!gpsReady()) return
  state.beaconAt = state.position
  state.beaconStop = startBeacon(meter, (result) => {
    store.addRecord('ref', {
      t: result.startedAt,
      lat: state.beaconAt.lat,
      lon: state.beaconAt.lon,
      acc: state.beaconAt.acc,
      name: 'beacon',
      l10: result.l10,
      mean: result.mean,
      wind: result.wind,
    })
    renderData()
  })
  renderStatus()
}

async function ensureMic() {
  if (meter.running) return true
  try {
    await meter.start()
    renderStatus()
    return true
  } catch (error) {
    showBanner('bad', `เปิดไมค์ไม่ได้ ${error.message}`)
    return false
  }
}

function watchPosition() {
  navigator.geolocation.watchPosition(
    (fix) => {
      state.position = { lat: fix.coords.latitude, lon: fix.coords.longitude, acc: fix.coords.accuracy }
      mapView.updatePosition(state.position.lat, state.position.lon, state.position.acc)
      renderStatus()
    },
    (error) => showBanner('bad', `GPS ใช้ไม่ได้ ${error.message}`),
    { enableHighAccuracy: true, maximumAge: 0, timeout: 30000 },
  )
}

async function runPrefetch() {
  if (!state.position) return
  el('btnPrefetch').disabled = true
  try {
    const total = await prefetchTiles(state.position, (done, all) => {
      el('prefetchState').textContent = `กำลังโหลด tile ${done} จาก ${all}`
    })
    el('prefetchState').textContent = `เก็บ tile ไว้แล้ว ${total} รูป ใช้ได้ตอนไม่มีเน็ต`
  } catch (error) {
    el('prefetchState').textContent = error.message
  } finally {
    el('btnPrefetch').disabled = false
  }
}

async function importCsv(file) {
  const added = store.mergeRecords(parseCsv(await file.text()))
  showBanner('warn', `import เข้ามา ${added} แถว`)
  renderData()
}

function bindEvents() {
  document.querySelectorAll('nav.tabs button').forEach((tab) => {
    tab.addEventListener('click', () => setScreen(tab.dataset.screen))
  })
  el('btnMeasure').addEventListener('click', async () => {
    if (await ensureMic()) await measureOnce()
  })
  el('btnSavePoint').addEventListener('click', () => savePending('point'))
  el('btnSetRef').addEventListener('click', () => savePending('ref'))
  el('btnBeacon').addEventListener('click', async () => {
    if (state.beaconStop || (await ensureMic())) toggleBeacon()
  })
  el('btnLocate').addEventListener('click', () => {
    if (state.position) mapView.centreOn(state.position.lat, state.position.lon)
  })
  el('btnPrefetch').addEventListener('click', runPrefetch)
  el('btnExportPoints').addEventListener('click', () => {
    downloadCsv(`base-scanner-points-${stamp()}.csv`, toCsv(anchorPoints(store.points, store.refs)))
  })
  el('btnExportRefs').addEventListener('click', () => {
    downloadCsv(`base-scanner-beacon-${stamp()}.csv`, toCsv(store.refs))
  })
  el('importFile').addEventListener('change', (event) => {
    if (event.target.files[0]) importCsv(event.target.files[0])
  })
  el('btnSetHome').addEventListener('click', () => {
    if (!state.position) return
    store.setHome({ lat: state.position.lat, lon: state.position.lon })
    state.bestTrail = []
    renderData()
  })
  el('btnClear').addEventListener('click', () => {
    if (!confirm('ลบจุดที่วัดและจุดอ้างอิงทั้งหมดในเครื่องนี้')) return
    store.clearAll()
    state.bestTrail = []
    renderData()
  })
  window.addEventListener('resize', sizeSpectrum)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.running) keepScreenAwake()
  })
}

function start() {
  store.load()
  mapView = new MapView(el('map-canvas'))
  bindEvents()
  sizeSpectrum()
  watchPosition()
  renderData()
  if (store.home) mapView.centreOn(store.home.lat, store.home.lon)
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js')
}

start()
