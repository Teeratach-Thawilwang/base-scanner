const GRID_SPAN_M = 6000
const CELL_M = 25
const GRID_SIZE = GRID_SPAN_M / CELL_M
const CELL_COUNT = GRID_SIZE * GRID_SIZE
const ORIGIN_M = -GRID_SPAN_M / 2
const DECAY_EXPONENTS = [1, 1.25, 1.5, 1.75, 2, 2.25, 2.5]
const SIGMA_DB = 2
const MIN_DISTANCE_M = 10
const CREDIBLE_MASS = 0.9
const MIN_POINTS = 4
const METERS_PER_DEGREE = 111320

export const thresholds = {
  MIN_POINTS,
  STOP_AREA_KM2: 0.05,
  STOP_MOVE_M: 50,
  OUTDOOR_MEANINGFUL_DB: 3,
}

export function toLocalMeters(home, lat, lon) {
  return {
    x: (lon - home.lon) * METERS_PER_DEGREE * Math.cos((home.lat * Math.PI) / 180),
    y: (lat - home.lat) * METERS_PER_DEGREE,
  }
}

export function toLatLon(home, x, y) {
  return {
    lat: home.lat + y / METERS_PER_DEGREE,
    lon: home.lon + x / (METERS_PER_DEGREE * Math.cos((home.lat * Math.PI) / 180)),
  }
}

export function cellCentre(index) {
  const ix = index % GRID_SIZE
  const iy = Math.floor(index / GRID_SIZE)
  return { x: ORIGIN_M + (ix + 0.5) * CELL_M, y: ORIGIN_M + (iy + 0.5) * CELL_M, ix, iy }
}

function usablePoints(points, home) {
  return points
    .filter((point) => point.anchored && point.delta !== null)
    .map((point) => ({ ...toLocalMeters(home, point.lat, point.lon), level: point.delta }))
}

function logLikelihoodGrid(local) {
  const grid = new Float32Array(CELL_COUNT * DECAY_EXPONENTS.length)
  let peak = -Infinity
  for (let k = 0; k < DECAY_EXPONENTS.length; k += 1) {
    const gain = 10 * DECAY_EXPONENTS[k]
    const layer = k * CELL_COUNT
    for (let iy = 0; iy < GRID_SIZE; iy += 1) {
      const sourceY = ORIGIN_M + (iy + 0.5) * CELL_M
      for (let ix = 0; ix < GRID_SIZE; ix += 1) {
        const sourceX = ORIGIN_M + (ix + 0.5) * CELL_M
        let sum = 0
        let sumOfSquares = 0
        for (let p = 0; p < local.length; p += 1) {
          const dx = local[p].x - sourceX
          const dy = local[p].y - sourceY
          const distance = Math.max(MIN_DISTANCE_M, Math.sqrt(dx * dx + dy * dy))
          const offset = local[p].level + gain * Math.log10(distance)
          sum += offset
          sumOfSquares += offset * offset
        }
        const residual = sumOfSquares - (sum * sum) / local.length
        const value = -residual / (2 * SIGMA_DB * SIGMA_DB)
        grid[layer + iy * GRID_SIZE + ix] = value
        if (value > peak) peak = value
      }
    }
  }
  return { grid, peak }
}

function collapseOverExponents(grid, peak) {
  const weights = new Float64Array(CELL_COUNT)
  const bestExponent = new Float32Array(CELL_COUNT)
  const bestScore = new Float64Array(CELL_COUNT)
  let total = 0
  for (let k = 0; k < DECAY_EXPONENTS.length; k += 1) {
    for (let index = 0; index < CELL_COUNT; index += 1) {
      const weight = Math.exp(grid[k * CELL_COUNT + index] - peak)
      weights[index] += weight
      total += weight
      if (weight > bestScore[index]) {
        bestScore[index] = weight
        bestExponent[index] = DECAY_EXPONENTS[k]
      }
    }
  }
  for (let index = 0; index < CELL_COUNT; index += 1) weights[index] /= total
  return { weights, bestExponent }
}

function credibleRegion(weights) {
  const order = Array.from({ length: CELL_COUNT }, (unused, index) => index).sort((a, b) => weights[b] - weights[a])
  const mask = new Uint8Array(CELL_COUNT)
  let mass = 0
  let count = 0
  for (const index of order) {
    if (mass >= CREDIBLE_MASS) break
    mask[index] = 1
    mass += weights[index]
    count += 1
  }
  return { mask, count, best: order[0], areaKm2: (count * CELL_M * CELL_M) / 1e6 }
}

export function solve(points, home) {
  const local = usablePoints(points, home)
  if (local.length < MIN_POINTS) return null

  const { grid, peak } = logLikelihoodGrid(local)
  const { weights, bestExponent } = collapseOverExponents(grid, peak)
  const region = credibleRegion(weights)
  const centre = cellCentre(region.best)

  return {
    size: GRID_SIZE,
    cellM: CELL_M,
    originM: ORIGIN_M,
    home,
    weights,
    bestExponent,
    mask: region.mask,
    credibleCells: region.count,
    areaKm2: region.areaKm2,
    pointCount: local.length,
    best: {
      ...centre,
      ...toLatLon(home, centre.x, centre.y),
      weight: weights[region.best],
      exponent: bestExponent[region.best],
      distanceFromHomeM: Math.sqrt(centre.x * centre.x + centre.y * centre.y),
    },
    bounds: {
      south: toLatLon(home, 0, ORIGIN_M).lat,
      north: toLatLon(home, 0, ORIGIN_M + GRID_SPAN_M).lat,
      west: toLatLon(home, ORIGIN_M, 0).lon,
      east: toLatLon(home, ORIGIN_M + GRID_SPAN_M, 0).lon,
    },
  }
}

export function suspects(solution, maxCount) {
  const inside = []
  for (let index = 0; index < CELL_COUNT; index += 1) {
    if (solution.mask[index]) inside.push(index)
  }
  inside.sort((a, b) => solution.weights[b] - solution.weights[a])
  const kept = inside.slice(0, maxCount)
  const total = kept.reduce((sum, index) => sum + solution.weights[index], 0)
  return kept.map((index) => ({
    ...cellCentre(index),
    exponent: solution.bestExponent[index],
    weight: solution.weights[index] / total,
  }))
}
