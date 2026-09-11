const FFT_SIZE = 16384
const SIGNAL_BAND_HZ = [30, 120]
const WIND_BAND_HZ = [4, 20]
const SPECTRUM_TOP_HZ = 200
const SILENT_FLOOR_DB = -160

const RAW_MIC_CONSTRAINTS = {
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: false,
}

function bandDb(bins, [lo, hi]) {
  let power = 0
  for (let i = lo; i <= hi; i += 1) power += 10 ** (bins[i] / 10)
  return power > 0 ? 10 * Math.log10(power) : SILENT_FLOOR_DB
}

export class AudioMeter {
  #context = null
  #analyser = null
  #stream = null
  #bins = null
  #signalRange = null
  #windRange = null
  #spectrumBinCount = 0

  get running() {
    return this.#analyser !== null
  }

  get sampleRate() {
    return this.#context ? this.#context.sampleRate : 0
  }

  get binWidthHz() {
    return this.sampleRate / FFT_SIZE
  }

  get spectrumTopHz() {
    return SPECTRUM_TOP_HZ
  }

  get signalBandHz() {
    return SIGNAL_BAND_HZ
  }

  async start() {
    if (this.running) return
    this.#stream = await navigator.mediaDevices.getUserMedia({ audio: RAW_MIC_CONSTRAINTS })
    this.#context = new AudioContext()
    await this.#context.resume()
    this.#analyser = this.#context.createAnalyser()
    this.#analyser.fftSize = FFT_SIZE
    this.#analyser.smoothingTimeConstant = 0
    this.#context.createMediaStreamSource(this.#stream).connect(this.#analyser)
    this.#bins = new Float32Array(this.#analyser.frequencyBinCount)
    this.#signalRange = this.#binRange(SIGNAL_BAND_HZ)
    this.#windRange = this.#binRange(WIND_BAND_HZ)
    this.#spectrumBinCount = this.#binIndex(SPECTRUM_TOP_HZ) + 1
  }

  stop() {
    if (this.#stream) this.#stream.getTracks().forEach((track) => track.stop())
    if (this.#context) this.#context.close()
    this.#context = null
    this.#analyser = null
    this.#stream = null
    this.#bins = null
  }

  readFrame() {
    this.#analyser.getFloatFrequencyData(this.#bins)
    return {
      signalDb: bandDb(this.#bins, this.#signalRange),
      windDb: bandDb(this.#bins, this.#windRange),
      spectrum: this.#bins.subarray(0, this.#spectrumBinCount),
    }
  }

  activeProcessing() {
    if (!this.#stream) return []
    const settings = this.#stream.getAudioTracks()[0].getSettings()
    return Object.keys(RAW_MIC_CONSTRAINTS).filter((key) => settings[key] === true)
  }

  #binIndex(hz) {
    return Math.round(hz / this.binWidthHz)
  }

  #binRange([lowHz, highHz]) {
    return [Math.max(1, this.#binIndex(lowHz)), this.#binIndex(highHz)]
  }
}
