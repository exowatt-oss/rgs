import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { openEventsChannel, type EventsChannel } from '@/lib/live/eventsChannel'
import { FakeWebSocket } from '@/test/fakeWebSocket'

const ts = '2026-10-29T17:05:12Z'

let channel: EventsChannel | null = null
beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
})
afterEach(() => {
  channel?.close()
  channel = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function connect(opts: { sinceSeq?: () => number; onTrouble?: () => void; onAuthLapse?: () => void } = {}) {
  const onEvents = vi.fn()
  const onEnded = vi.fn()
  channel = openEventsChannel({
    sessionId: 'ses-1',
    transport: 'ws',
    sinceSeq: opts.sinceSeq ?? (() => 0),
    onEvents,
    onEnded,
    onStateChange: () => {},
    onTrouble: opts.onTrouble,
    onAuthLapse: opts.onAuthLapse,
    expectHeartbeat: () => false,
    createSocket: (url) => new FakeWebSocket(url) as unknown as WebSocket,
  })
  return { onEvents, onEnded }
}

describe('events WebSocket close contract', () => {
  it('ends on 4404 whether the session is missing or not readable', () => {
    const { onEvents, onEnded } = connect()
    const ws = FakeWebSocket.last('/events')
    ws.open()
    ws.receive({ type: 'error', seq: null, ts, code: 'not_found', close_code: 4404, message: 'Not found.' })
    expect(onEvents).toHaveBeenCalledWith([{
      type: 'error', seq: null, ts: Date.parse(ts), code: 'not_found', closeCode: 4404, message: 'Not found.',
    }])
    ws.serverClose(4404)
    vi.advanceTimersByTime(30_000)
    expect(onEnded).toHaveBeenCalledOnce()
    expect(onEnded).toHaveBeenCalledWith('not_found')
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('still ends on 4404 when the error frame is lost', () => {
    const { onEnded } = connect()
    const ws = FakeWebSocket.last('/events')
    ws.open()
    ws.serverClose(4404)
    expect(onEnded).toHaveBeenCalledWith('not_found')
  })

  it('marks a framed 4401 as an auth lapse without probing REST, and reconnects', () => {
    const onTrouble = vi.fn()
    const onAuthLapse = vi.fn()
    const { onEnded } = connect({ onTrouble, onAuthLapse })
    const ws = FakeWebSocket.last('/events')
    ws.open()
    ws.receive({ type: 'error', seq: null, ts, code: 'unauthenticated', close_code: 4401, message: 'Not signed in.' })
    ws.serverClose(4401)
    expect(onAuthLapse).toHaveBeenCalledOnce()
    expect(onTrouble).not.toHaveBeenCalled()
    expect(onEnded).not.toHaveBeenCalled()
    vi.advanceTimersByTime(5_000)
    expect(FakeWebSocket.last('/events')).not.toBe(ws)
  })

  it('probes status only as fallback on an unframed 4401', () => {
    const onTrouble = vi.fn()
    const onAuthLapse = vi.fn()
    connect({ onTrouble, onAuthLapse })
    const ws = FakeWebSocket.last('/events')
    ws.open()
    ws.serverClose(4401)
    expect(onTrouble).toHaveBeenCalledOnce()
    expect(onAuthLapse).not.toHaveBeenCalled()
  })

  it('replays after 4408 resync from the last applied seq, without treating it as a failure', () => {
    let applied = 3
    const onTrouble = vi.fn()
    const { onEnded, onEvents } = connect({ sinceSeq: () => applied, onTrouble })
    const ws = FakeWebSocket.last('/events')
    expect(ws.url).toContain('since_seq=3')
    ws.open()
    ws.receive({ type: 'resync', seq: null, ts, data: { last_seq: 20 } })
    expect(onEvents).toHaveBeenCalledWith([expect.objectContaining({ type: 'resync', seq: null, ts: Date.parse(ts), lastSeq: 20 })])
    applied = 7 // only applied durable events, not the resync hint
    ws.receive({ type: 'error', seq: null, ts, code: 'resync', close_code: 4408, message: 'Slow client.' })
    ws.serverClose(4408)
    vi.advanceTimersByTime(5_000)
    expect(FakeWebSocket.last('/events').url).toContain('since_seq=7')
    expect(onTrouble).not.toHaveBeenCalled()
    expect(onEnded).not.toHaveBeenCalled()
  })

  it('reconnects an unexpected drop even after finalization (WS stream stays open)', () => {
    const { onEnded } = connect()
    const ws = FakeWebSocket.last('/events')
    ws.open()
    ws.receive({ type: 'snapshot', seq: null, ts, data: { session: { id: 'ses-1', status: 'finalized' }, last_seq: 7, interim: null } })
    ws.serverClose(1006)
    vi.advanceTimersByTime(5_000)
    expect(FakeWebSocket.last('/events')).not.toBe(ws)
    expect(onEnded).not.toHaveBeenCalled()
  })
})
