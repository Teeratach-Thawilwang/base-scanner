const RUN_MS = 30000
const FRAME_MS = 100
const L10_PERCENTILE = 0.9
const WIND_MARGIN_DB = 3
const ANCHOR_MAX_GAP_MS = 5 * 60 * 1000
const REF_DRIFT_DB = 3
const BEACON_PERIOD_MS = 30000

function percentile(values, fraction) {
  const sorted = [...values].sort((a, b) => a - b)
  return sorted[Math.floor(fraction * (sorted.length - 1))]
}

function powerMeanDb(values) {
  const power = values.reduce((sum, db) => sum + 10 ** (db / 10), 0) / values.length
  return 10 * Math.log10(power)
}

function summarise(startedAt, signalDbs, windDbs) {
  const l10 = percentile(signalDbs, L10_PERCENTILE)
  const windL10 = percentile(windDbs, L10_PERCENTILE)
  return {
    startedAt,
    endedAt: Date.now(),
    frames: signalDbs.length,
    l10,
    mean: powerMeanDb(signalDbs),
    wind: windL10,
    windMargin: l10 - windL10,
    windy: l10 - windL10 < WIND_MARGIN_DB,
  }
}

export function startMeasurement(meter, { onFrame } = {}) {
  const startedAt = Date.now()
  const signalDbs = []
  const windDbs = []
  let timer = null
  let settle = null

  const promise = new Promise((resolve, reject) => {
    settle = { resolve, reject }
    timer = setInterval(() => {
      const frame = meter.readFrame()
      signalDbs.push(frame.signalDb)
      windDbs.push(frame.windDb)
      const elapsed = Date.now() - startedAt
      if (onFrame) onFrame(frame, Math.max(0, RUN_MS - elapsed))
      if (elapsed < RUN_MS) return
      clearInterval(timer)
      resolve(summarise(startedAt, signalDbs, windDbs))
    }, FRAME_MS)
  })

  return {
    done: promise,
    cancel() {
      clearInterval(timer)
      settle.reject(new Error('cancelled'))
    },
  }
}

export function startBeacon(meter, onReading) {
  let run = null
  let timer = null
  let stopped = false

  const cycle = async () => {
    run = startMeasurement(meter)
    let result = null
    try {
      result = await run.done
    } catch {
      return
    }
    if (stopped) return
    onReading(result)
    if (!stopped) timer = setTimeout(cycle, BEACON_PERIOD_MS - RUN_MS)
  }
  cycle()

  return () => {
    stopped = true
    clearTimeout(timer)
    if (run) run.cancel()
  }
}

export function referenceLevelAt(refs, time) {
  if (refs.length === 0) return null
  const sorted = [...refs].sort((a, b) => a.t - b.t)
  const after = sorted.findIndex((ref) => ref.t >= time)
  if (after === 0) return { db: sorted[0].l10, gapMs: sorted[0].t - time }
  if (after < 0) {
    const last = sorted[sorted.length - 1]
    return { db: last.l10, gapMs: time - last.t }
  }
  const before = sorted[after - 1]
  const next = sorted[after]
  const span = next.t - before.t
  const ratio = span === 0 ? 0 : (time - before.t) / span
  return {
    db: before.l10 + (next.l10 - before.l10) * ratio,
    gapMs: Math.min(time - before.t, next.t - time),
  }
}

export function anchorPoints(points, refs) {
  return points.map((point) => {
    const reference = referenceLevelAt(refs, point.t)
    const anchored = reference !== null && reference.gapMs <= ANCHOR_MAX_GAP_MS
    return {
      ...point,
      anchored,
      gapMs: reference ? reference.gapMs : Infinity,
      delta: reference ? point.l10 - reference.db : null,
    }
  })
}

export function referenceDrift(refs) {
  if (refs.length < 2) return 0
  const levels = refs.map((ref) => ref.l10)
  return Math.max(...levels) - Math.min(...levels)
}

export function referenceDrifted(refs) {
  return referenceDrift(refs) > REF_DRIFT_DB
}

export const timing = { RUN_MS, BEACON_PERIOD_MS, ANCHOR_MAX_GAP_MS }
