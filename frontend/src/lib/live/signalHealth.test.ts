import { describe, expect, it } from 'vitest'

import { SILENT_AFTER_MS, SILENT_AFTER_SOUND_MS, STALL_MS, SignalHealth } from '@/lib/live/signalHealth'

/** Feed ~20 Hz level reports of `rms` from `from` to `to` ms. */
function feed(h: SignalHealth, rms: number, from: number, to: number) {
  for (let t = from; t <= to; t += 50) h.level(rms, t)
}

describe('SignalHealth', () => {
  it('flags exact digital zeros after 3 s and clears as soon as sound arrives', () => {
    const h = new SignalHealth(0)
    feed(h, 0, 0, SILENT_AFTER_MS - 100)
    expect(h.problem(SILENT_AFTER_MS - 100)).toBeNull()
    feed(h, 0, SILENT_AFTER_MS - 50, SILENT_AFTER_MS + 200)
    expect(h.problem(SILENT_AFTER_MS + 200)).toBe('silent')
    h.level(0.02, SILENT_AFTER_MS + 250)
    expect(h.problem(SILENT_AFTER_MS + 250)).toBeNull()
  })

  it('tolerates a quiet room and pauses once real sound was heard', () => {
    const h = new SignalHealth(0)
    feed(h, 3e-4, 0, 10_000) // -70 dBFS noise floor: a working mic
    expect(h.problem(10_000)).toBeNull()
    feed(h, 0, 10_050, 10_050 + SILENT_AFTER_SOUND_MS - 100) // noise-gated pause
    expect(h.problem(10_050 + SILENT_AFTER_SOUND_MS - 100)).toBeNull()
    feed(h, 0, 10_050 + SILENT_AFTER_SOUND_MS, 10_050 + SILENT_AFTER_SOUND_MS + 50)
    expect(h.problem(10_100 + SILENT_AFTER_SOUND_MS)).toBe('silent')
  })

  it('reports a stalled graph, a muted track and an ended track', () => {
    const h = new SignalHealth(0)
    feed(h, 0.01, 0, 1_000)
    expect(h.problem(1_000 + STALL_MS + 1)).toBe('stalled')
    h.muted = true
    expect(h.problem(1_100)).toBe('muted')
    h.ended = true
    expect(h.problem(1_100)).toBe('ended')
    h.reset(2_000)
    expect(h.problem(2_000)).toBeNull()
  })
})
