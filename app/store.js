const STORAGE_KEY = 'base-scanner.v1'
const CSV_COLUMNS = ['time', 'lat', 'lon', 'accuracy', 'name', 'l10', 'mean', 'delta', 'wind', 'kind']

function emptyState() {
  return { home: null, points: [], refs: [] }
}

function newId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

function csvCell(value) {
  const text = value === null || value === undefined ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function splitCsvLine(line) {
  const cells = []
  let cell = ''
  let quoted = false
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i]
    if (quoted && char === '"' && line[i + 1] === '"') {
      cell += '"'
      i += 1
    } else if (char === '"') {
      quoted = !quoted
    } else if (char === ',' && !quoted) {
      cells.push(cell)
      cell = ''
    } else {
      cell += char
    }
  }
  cells.push(cell)
  return cells
}

function round(value, digits) {
  return value === null || value === undefined || Number.isNaN(value) ? '' : Number(value.toFixed(digits))
}

export class Store {
  #state = emptyState()

  load() {
    const raw = localStorage.getItem(STORAGE_KEY)
    this.#state = raw ? { ...emptyState(), ...JSON.parse(raw) } : emptyState()
    return this.#state
  }

  get home() {
    return this.#state.home
  }

  get points() {
    return this.#state.points
  }

  get refs() {
    return this.#state.refs
  }

  setHome(position) {
    this.#state.home = position
    this.#save()
  }

  addRecord(kind, record) {
    const stored = { id: newId(), kind, ...record }
    const bucket = kind === 'ref' ? this.#state.refs : this.#state.points
    bucket.push(stored)
    if (!this.#state.home) this.#state.home = { lat: stored.lat, lon: stored.lon }
    this.#save()
    return stored
  }

  removeRecord(id) {
    this.#state.points = this.#state.points.filter((record) => record.id !== id)
    this.#state.refs = this.#state.refs.filter((record) => record.id !== id)
    this.#save()
  }

  clearAll() {
    this.#state = emptyState()
    this.#save()
  }

  mergeRecords(records) {
    const known = new Set([...this.#state.points, ...this.#state.refs].map((record) => `${record.kind}:${record.t}`))
    let added = 0
    records.forEach((record) => {
      const key = `${record.kind}:${record.t}`
      if (known.has(key)) return
      known.add(key)
      const bucket = record.kind === 'ref' ? this.#state.refs : this.#state.points
      bucket.push({ id: newId(), ...record })
      added += 1
    })
    this.#save()
    return added
  }

  #save() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(this.#state))
  }
}

export function toCsv(records) {
  const rows = records.map((record) =>
    [
      new Date(record.t).toISOString(),
      round(record.lat, 6),
      round(record.lon, 6),
      round(record.acc, 1),
      record.name,
      round(record.l10, 2),
      round(record.mean, 2),
      round(record.delta, 2),
      round(record.wind, 2),
      record.kind,
    ]
      .map(csvCell)
      .join(','),
  )
  return [CSV_COLUMNS.join(','), ...rows].join('\n')
}

export function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/)
  const header = splitCsvLine(lines[0]).map((cell) => cell.trim().toLowerCase())
  const columnOf = (name) => header.indexOf(name)
  return lines.slice(1).map((line) => {
    const cells = splitCsvLine(line)
    const value = (name) => cells[columnOf(name)]
    const number = (name) => (value(name) === '' || value(name) === undefined ? null : Number(value(name)))
    return {
      t: Date.parse(value('time')),
      lat: number('lat'),
      lon: number('lon'),
      acc: number('accuracy'),
      name: value('name') || '',
      l10: number('l10'),
      mean: number('mean'),
      wind: number('wind'),
      kind: value('kind') === 'ref' ? 'ref' : 'point',
    }
  })
}

export function downloadCsv(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}
