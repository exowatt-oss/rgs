import { afterEach, describe, expect, it, vi } from 'vitest'
import { copperVoice, PhononStream, toWire, type CopperFinal, type CopperState, type OnDeviceInfo } from '@/lib/asr/copperPhonon'

function fakeVoice(state: Partial<CopperState> = {}) {
  const pushed: number[] = []
  let handlers: {
    onFinal?: (e: CopperFinal) => void
    onEnd?: (e: { reason: string }) => void
  } = {}
  const voice = {
    version: 1,
    engine: 'phonon-2',
    state: async (): Promise<CopperState> => ({
      ok: true,
      available: true,
      enabled: true,
      ready: true,
      model: 'ready',
      consent: 'granted',
      engine: 'phonon-2',
      ...state,
    }),
    listen: async (opts: typeof handlers) => {
      handlers = opts
      return {
        id: 1,
        push: (pcm: ArrayBuffer) => pushed.push(pcm.byteLength / 2),
        stop: async () => handlers.onEnd?.({ reason: 'stopped' }),
        cancel: () => {},
      }
    },
  }
  return { voice, pushed, final: (f: CopperFinal) => handlers.onFinal?.(f) }
}

afterEach(() => {
  delete (window as unknown as { copper?: unknown }).copper
})

describe('copperVoice', () => {
  it('is null outside Copper and found inside', () => {
    expect(copperVoice()).toBeNull()
    ;(window as unknown as { copper: unknown }).copper = {
      voice: fakeVoice().voice,
    }
    expect(copperVoice()).not.toBeNull()
  })
})

describe('toWire', () => {
  it('moves Copper times onto the session timeline', () => {
    const w = toWire(
      {
        type: 'final',
        seq: 0,
        start: 1,
        end: 2.5,
        text: 'hi there',
        words: [{ text: 'hi', start: 1.1, end: 1.3 }],
      },
      10,
    )
    expect(w).toEqual({
      seq: 11000,
      start_s: 11,
      end_s: 12.5,
      text: 'hi there',
      words: [{ text: 'hi', start_s: 11.1, end_s: 11.3 }],
    })
  })
})

describe('PhononStream', () => {
  it('feeds frames, offsets by what the uplink had before, posts finals', async () => {
    const { voice, pushed, final } = fakeVoice()
    const posts: unknown[] = []
    let sessionSamples = 16000 * 5 // 5 s already in the uplink before Copper listened
    const infos: OnDeviceInfo[] = []
    const stream = new PhononStream({
      sessionId: 'abc',
      sessionSamples: () => sessionSamples,
      onChange: (i) => infos.push(i),
      voice,
      post: async (_id, body) => {
        posts.push(body)
      },
    })
    await stream.open()
    expect(stream.status.status).toBe('on')
    const frame = new ArrayBuffer(3200) // 100 ms
    for (let i = 0; i < 20; i++) {
      sessionSamples += 1600
      stream.push(frame)
    }
    expect(pushed.reduce((a, b) => a + b, 0)).toBe(32000)
    final({
      type: 'final',
      seq: 0,
      start: 0.5,
      end: 1.5,
      text: 'hello',
      words: [{ text: 'hello', start: 0.6, end: 1.0 }],
    })
    await vi.waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({
      engine: 'phonon-2',
      segments: [
        {
          seq: 5500,
          start_s: 5.5,
          end_s: 6.5,
          text: 'hello',
          words: [{ text: 'hello', start_s: 5.6, end_s: 6 }],
        },
      ],
    })
    await stream.stop()
    expect(stream.status.phrases).toBe(1)
  })

  it('says why when Copper voice is off, and does nothing', async () => {
    const { voice } = fakeVoice({ enabled: false })
    const stream = new PhononStream({
      sessionId: 'abc',
      sessionSamples: () => 0,
      onChange: () => {},
      voice,
      post: async () => {},
    })
    await stream.open()
    expect(stream.status.status).toBe('unavailable')
    expect(stream.status.reason).toMatch(/Voice/)
    stream.push(new ArrayBuffer(3200))
  })

  it('outside Copper is absent', async () => {
    const stream = new PhononStream({
      sessionId: 'abc',
      sessionSamples: () => 0,
      onChange: () => {},
      voice: null,
    })
    await stream.open()
    expect(stream.status.status).toBe('absent')
  })
})
