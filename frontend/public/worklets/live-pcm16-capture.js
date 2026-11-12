/*
 * Live Record capture worklet (AudioWorkletGlobalScope).
 *
 * Input:  the mixed mic (+ optional Meet tab) signal at the AudioContext's
 *         native rate (44.1/48 kHz), already low-passed by two native
 *         BiquadFilterNodes in the graph (anti-alias before decimation).
 * Output: fixed-size frames of little-endian signed 16-bit mono PCM at
 *         `targetSampleRate`, posted to the main thread as transferable
 *         ArrayBuffers: { type: 'frame', buffer }.
 *         Plus ~20 Hz level reports: { type: 'level', rms, peak }.
 *
 * Plain ES2017 with no imports on purpose: it is served as a static file
 * from /worklets/ so it loads identically in Chrome and Safari without any
 * bundler worker transform.
 *
 * Messages in: { type: 'flush' } → posts any partial frame, then
 *              { type: 'flushed' }.
 */
/* global sampleRate, registerProcessor, AudioWorkletProcessor */
class LivePcm16CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opts = (options && options.processorOptions) || {}
    this.targetRate = opts.targetSampleRate || 16000
    this.frameSamples = opts.frameSamples || 1600
    this.ratio = sampleRate / this.targetRate
    // Fractional read position in the current block's sample coordinates;
    // -1 addresses the last sample of the previous block (`this.prev`).
    this.pos = 0
    this.prev = 0
    this.mono = new Float32Array(128)
    this.newFrame()
    this.levelSumSq = 0
    this.levelCount = 0
    this.levelPeak = 0
    this.levelEvery = Math.max(1, Math.round(sampleRate / 20))
    this.port.onmessage = (event) => {
      const data = event.data || {}
      if (data.type === 'flush') {
        if (this.fill > 0) this.emit(this.fill)
        this.port.postMessage({ type: 'flushed' })
      }
    }
  }

  newFrame() {
    this.buffer = new ArrayBuffer(this.frameSamples * 2)
    this.view = new DataView(this.buffer)
    this.fill = 0
  }

  emit(samples) {
    const buffer = samples === this.frameSamples ? this.buffer : this.buffer.slice(0, samples * 2)
    this.port.postMessage({ type: 'frame', buffer }, [buffer])
    this.newFrame()
  }

  push(sample) {
    const s = sample > 1 ? 1 : sample < -1 ? -1 : sample
    this.view.setInt16(this.fill * 2, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true)
    this.fill += 1
    if (this.fill === this.frameSamples) this.emit(this.frameSamples)
  }

  process(inputs) {
    const input = inputs[0]
    if (!input || input.length === 0 || !input[0]) return true
    const n = input[0].length
    if (this.mono.length !== n) this.mono = new Float32Array(n)
    const mono = this.mono
    const channels = input.length
    for (let i = 0; i < n; i++) {
      let sum = 0
      for (let c = 0; c < channels; c++) sum += input[c][i]
      const v = sum / channels
      mono[i] = v
      this.levelSumSq += v * v
      const a = v < 0 ? -v : v
      if (a > this.levelPeak) this.levelPeak = a
    }
    this.levelCount += n
    if (this.levelCount >= this.levelEvery) {
      this.port.postMessage({
        type: 'level',
        rms: Math.sqrt(this.levelSumSq / this.levelCount),
        peak: this.levelPeak,
      })
      this.levelSumSq = 0
      this.levelCount = 0
      this.levelPeak = 0
    }
    // Linear-interpolation decimation.
    let p = this.pos
    const last = n - 1
    while (p <= last) {
      const i = Math.floor(p)
      const frac = p - i
      const s0 = i < 0 ? this.prev : mono[i]
      const s1 = i + 1 <= last ? mono[i + 1] : s0
      this.push(s0 + (s1 - s0) * frac)
      p += this.ratio
    }
    this.pos = p - n
    this.prev = mono[last]
    return true
  }
}

registerProcessor('live-pcm16-capture', LivePcm16CaptureProcessor)
