import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DEFAULT_AUDIO_FORMAT } from '@/api/live'
import { AudioUplink, SampleBuffer } from '@/lib/live/audioUplink'
import { FakeWebSocket } from '@/test/fakeWebSocket'

/** A frame of `n` samples whose values encode their absolute index (mod
 *  2^15), so a test can check exactly which samples were (re)sent. */
function frame(start: number, n: number): ArrayBuffer {
  const pcm = new Int16Array(n)
  for (let i = 0; i < n; i += 1) pcm[i] = (start + i) % 32768
  return pcm.buffer
}
const firstSample = (data: ArrayBuffer) => new Int16Array(data)[0]
const samples = (data: ArrayBuffer) => data.byteLength / 2

describe('SampleBuffer', () => {
  it('peeks contiguous audio from the cursor, coalescing frames up to a byte cap', () => {
    const b = new SampleBuffer(1 << 20)
    b.push(frame(0, 1600))
    b.push(frame(1600, 1600))
    b.push(frame(3200, 1600))
    const first = b.peek(4000)!
    expect(first.offset).toBe(0)
    expect(first.data.byteLength).toBe(4000)
    b.advance(first.data.byteLength)
    const rest = b.peek(1 << 16)!
    expect(rest.offset).toBe(2000)
    expect(firstSample(rest.data)).toBe(2000)
    expect(samples(rest.data)).toBe(4800 - 2000)
  })

  it('frees audio on ack and resumes exactly at the server offset', () => {
    const b = new SampleBuffer(1 << 20)
    for (let i = 0; i < 5; i += 1) b.push(frame(i * 1600, 1600))
    b.advance(8000 * 2) // everything "sent"
    b.ack(3200)
    expect(b.start).toBe(3200)
    expect(b.unackedSamples).toBe(4800)

    // Reconnect: the server durably holds 4000 — resend from there only.
    b.resumeFrom(4000)
    const next = b.peek(1 << 20)!
    expect(next.offset).toBe(4000)
    expect(firstSample(next.data)).toBe(4000)
    expect(samples(next.data)).toBe(4000)
  })

  it('drops the oldest unacked audio past the cap and rebases on resume', () => {
    const b = new SampleBuffer(3 * 1600 * 2) // three frames
    for (let i = 0; i < 5; i += 1) b.push(frame(i * 1600, 1600))
    expect(b.droppedSamples).toBe(3200)
    expect(b.start).toBe(3200)

    // Server holds only 1000 samples: it can't get 1000..3200 any more, so
    // the stream continues at 1000 with the oldest audio we still have.
    b.resumeFrom(1000)
    const next = b.peek(1 << 20)!
    expect(next.offset).toBe(1000)
    expect(firstSample(next.data)).toBe(3200)
    expect(b.end).toBe(1000 + 3 * 1600)
  })

  it('jumps forward when the server is ahead (fed by another uplink)', () => {
    const b = new SampleBuffer(1 << 20)
    b.push(frame(0, 1600))
    b.resumeFrom(50_000)
    expect(b.peek(1 << 20)).toBeNull()
    b.push(frame(1600, 160))
    expect(b.peek(1 << 20)!.offset).toBe(50_000)
  })
})

describe('AudioUplink over WebSocket', () => {
  beforeEach(() => {
    FakeWebSocket.reset()
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  function make(onEnded = vi.fn()) {
    const uplink = new AudioUplink({
      sessionId: 'ses-1',
      format: DEFAULT_AUDIO_FORMAT,
      transport: 'ws',
      maxBufferBytes: 1 << 22,
      onChange: () => {},
      onEnded,
      createSocket: (url) => new FakeWebSocket(url) as unknown as WebSocket,
    })
    return { uplink, onEnded }
  }

  it('waits for ready, resumes across a drop without gaps or duplicates', () => {
    const { uplink } = make()
    uplink.push(frame(0, 1600)) // pre-roll before the socket exists
    uplink.start()
    const ws1 = FakeWebSocket.last('/audio')
    expect(ws1.url).toMatch(/\/api\/live\/sessions\/ses-1\/audio$/)
    ws1.open()
    uplink.push(frame(1600, 1600))
    expect(ws1.binarySent).toHaveLength(0) // nothing before `ready`

    ws1.receive({ type: 'ready', received_samples: 0 })
    expect(ws1.binarySent.map((d) => [firstSample(d), samples(d)])).toEqual([[0, 3200]])

    uplink.push(frame(3200, 1600))
    ws1.receive({ type: 'ack', received_samples: 3200 })
    expect(uplink.buffer.acked).toBe(3200)

    // Network drop: the server only persisted up to 4000.
    ws1.serverClose(1006)
    uplink.push(frame(4800, 1600))
    vi.advanceTimersByTime(5_000)
    const ws2 = FakeWebSocket.last('/audio')
    expect(ws2).not.toBe(ws1)
    ws2.open()
    ws2.receive({ type: 'ready', received_samples: 4000 })
    const resent = ws2.binarySent
    expect(firstSample(resent[0])).toBe(4000)
    expect(resent.reduce((n, d) => n + samples(d), 0)).toBe(6400 - 4000)
    uplink.close()
  })

  it.each([
    [4409, 'not_live', 'stopped'],
    [4410, 'superseded', 'superseded'],
    [4403, 'forbidden', 'not_owner'],
    [4404, 'not_found', 'not_found'],
    [4400, 'bad_frame', 'bad_frame'],
    [4503, 'unavailable', 'unavailable'],
  ] as const)('honors the %i audio error frame (%s) without retry', (code, errorCode, reason) => {
    const { uplink, onEnded } = make()
    uplink.start()
    const ws = FakeWebSocket.last('/audio')
    ws.open()
    ws.receive({ type: 'error', code: errorCode, close_code: code, message: 'server says so', ts: new Date().toISOString() })
    ws.serverClose(code)
    vi.advanceTimersByTime(30_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(onEnded).toHaveBeenCalledWith(reason, 'server says so', { closeCode: code, hasErrorFrame: true })
    expect(uplink.stats.ended).toBe(reason)
  })

  it('uses error.code over the close code if the two disagree', () => {
    const { uplink, onEnded } = make()
    uplink.start()
    const ws = FakeWebSocket.last('/audio')
    ws.open()
    ws.receive({ type: 'error', code: 'superseded', close_code: 4410, message: 'new tab', ts: new Date().toISOString() })
    ws.serverClose(4409)
    expect(onEnded).toHaveBeenCalledWith('superseded', 'new tab', { closeCode: 4409, hasErrorFrame: true })
  })

  it.each([
    [4409, 'stopped'],
    [4410, 'superseded'],
  ] as const)('falls back to close code %i when the error frame was lost', (code, reason) => {
    const { uplink, onEnded } = make()
    uplink.start()
    const ws = FakeWebSocket.last('/audio')
    ws.open()
    ws.serverClose(code)
    expect(onEnded).toHaveBeenCalledWith(reason, null, { closeCode: code, hasErrorFrame: false })
  })

  it('retries a server_error (1011) and drops the previous error frame before the next connection', () => {
    const { uplink, onEnded } = make()
    uplink.start()
    const ws = FakeWebSocket.last('/audio')
    ws.open()
    ws.receive({ type: 'error', code: 'server_error', close_code: 1011, message: 'retry', ts: new Date().toISOString() })
    ws.serverClose(1011)
    vi.advanceTimersByTime(5_000)
    const retry = FakeWebSocket.last('/audio')
    expect(retry).not.toBe(ws)
    retry.open()
    retry.serverClose(4410) // this connection has no error frame
    expect(onEnded).toHaveBeenCalledWith('superseded', null, { closeCode: 4410, hasErrorFrame: false })
    expect(uplink.stats.ended).toBe('superseded')
  })

  it('keeps buffering and retrying through an SSO lapse (4401)', () => {
    const { uplink } = make()
    uplink.start()
    FakeWebSocket.last('/audio').open()
    FakeWebSocket.last('/audio').serverClose(4401)
    expect(uplink.stats.authLapsed).toBe(true)
    uplink.push(frame(0, 1600))
    vi.advanceTimersByTime(15_000)
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
    expect(uplink.stats.bufferedMs).toBe(100)
    uplink.close()
  })

  it('pings a quiet socket, then reconnects after a 5 s ack stall and resumes exactly', () => {
    const { uplink } = make()
    uplink.start()
    const ws1 = FakeWebSocket.last('/audio')
    ws1.open()
    ws1.receive({ type: 'ready', received_samples: 0 })
    uplink.push(frame(0, 1600))
    expect(ws1.binarySent).toHaveLength(1)

    // The server goes quiet (half-open socket): a ping first, no reconnect yet.
    vi.advanceTimersByTime(2_500)
    expect(ws1.sent).toContain(JSON.stringify({ type: 'ping' }))
    expect(FakeWebSocket.last('/audio')).toBe(ws1)

    // Still nothing past the stall window: drop it and reconnect at once.
    vi.advanceTimersByTime(3_000)
    const ws2 = FakeWebSocket.last('/audio')
    expect(ws2).not.toBe(ws1)
    expect(uplink.stats.state).toBe('reconnecting')
    ws2.open()
    ws2.receive({ type: 'ready', received_samples: 800 })
    expect(firstSample(ws2.binarySent[0])).toBe(800)
    uplink.close()
  })

  it('abandons a handshake that hangs', () => {
    const { uplink } = make()
    uplink.start()
    const ws1 = FakeWebSocket.last('/audio')
    vi.advanceTimersByTime(10_500)
    expect(FakeWebSocket.last('/audio')).not.toBe(ws1)
    uplink.close()
  })

  it('drain resolves once the server has acked everything', async () => {
    const { uplink } = make()
    uplink.start()
    const ws = FakeWebSocket.last('/audio')
    ws.open()
    ws.receive({ type: 'ready', received_samples: 0 })
    uplink.push(frame(0, 1600))
    const drained = uplink.drain(5_000)
    ws.receive({ type: 'ack', received_samples: 1600 })
    await vi.advanceTimersByTimeAsync(200)
    await expect(drained).resolves.toBe(true)
    uplink.close()
  })
})
