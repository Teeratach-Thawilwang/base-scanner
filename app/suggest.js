import { suspects, toLatLon, toLocalMeters } from './solve.js'

const CANDIDATE_STEP_M = 100
const CANDIDATE_RADIUS_M = 1500
const WALK_PENALTY_M = 300
const SUGGESTION_COUNT = 5
const MIN_SEPARATION_M = 150
const SUSPECT_SAMPLE = 400
const MIN_DISTANCE_M = 10
const FIRST_RING_RADIUS_M = 300

export const SUGGESTION_RADIUS_M = 50

const COMPASS = [
  'เหนือ',
  'ตะวันออกเฉียงเหนือ',
  'ตะวันออก',
  'ตะวันออกเฉียงใต้',
  'ใต้',
  'ตะวันตกเฉียงใต้',
  'ตะวันตก',
  'ตะวันตกเฉียงเหนือ',
]

function distance(ax, ay, bx, by) {
  return Math.max(MIN_DISTANCE_M, Math.sqrt((ax - bx) ** 2 + (ay - by) ** 2))
}

export function ringPoints(home, radiusM = FIRST_RING_RADIUS_M) {
  return COMPASS.map((name, index) => {
    const angle = (index / COMPASS.length) * 2 * Math.PI
    const x = radiusM * Math.sin(angle)
    const y = radiusM * Math.cos(angle)
    return { x, y, ...toLatLon(home, x, y), name, rank: index + 1, radiusM: SUGGESTION_RADIUS_M }
  })
}

function candidateGrid() {
  const steps = Math.floor(CANDIDATE_RADIUS_M / CANDIDATE_STEP_M)
  const candidates = []
  for (let iy = -steps; iy <= steps; iy += 1) {
    for (let ix = -steps; ix <= steps; ix += 1) {
      const x = ix * CANDIDATE_STEP_M
      const y = iy * CANDIDATE_STEP_M
      if (Math.sqrt(x * x + y * y) <= CANDIDATE_RADIUS_M) candidates.push({ x, y })
    }
  }
  return candidates
}

function informationScore(candidate, pool, reference) {
  let mean = 0
  let meanOfSquares = 0
  for (const suspect of pool) {
    const toReference = distance(reference.x, reference.y, suspect.x, suspect.y)
    const toCandidate = distance(candidate.x, candidate.y, suspect.x, suspect.y)
    const predicted = 10 * suspect.exponent * Math.log10(toReference / toCandidate)
    mean += suspect.weight * predicted
    meanOfSquares += suspect.weight * predicted * predicted
  }
  return meanOfSquares - mean * mean
}

function spreadOut(ranked) {
  const chosen = []
  for (const candidate of ranked) {
    if (chosen.length === SUGGESTION_COUNT) break
    const clashes = chosen.some(
      (picked) => Math.sqrt((picked.x - candidate.x) ** 2 + (picked.y - candidate.y) ** 2) < MIN_SEPARATION_M,
    )
    if (!clashes) chosen.push(candidate)
  }
  return chosen
}

export function suggestPoints({ solution, home, reference, standing }) {
  if (!solution) return ringPoints(home)

  const pool = suspects(solution, SUSPECT_SAMPLE)
  const referenceLocal = reference ? toLocalMeters(home, reference.lat, reference.lon) : { x: 0, y: 0 }
  const walkFrom = standing ? toLocalMeters(home, standing.lat, standing.lon) : { x: 0, y: 0 }

  const ranked = candidateGrid()
    .map((candidate) => {
      const walkM = Math.sqrt((candidate.x - walkFrom.x) ** 2 + (candidate.y - walkFrom.y) ** 2)
      return {
        ...candidate,
        walkM,
        score: informationScore(candidate, pool, referenceLocal) / (1 + walkM / WALK_PENALTY_M),
      }
    })
    .sort((a, b) => b.score - a.score)

  return spreadOut(ranked).map((candidate, index) => ({
    ...candidate,
    ...toLatLon(home, candidate.x, candidate.y),
    name: `จุดที่ ${index + 1}`,
    rank: index + 1,
    radiusM: SUGGESTION_RADIUS_M,
  }))
}
