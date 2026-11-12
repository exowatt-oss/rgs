/**
 * The live event stream (`README.md` §3) over either the
 * events WebSocket or the HTTP long-poll fallback, with one interface.
 *
 * Both resume from the last applied durable seq (`since_seq`), so a drop
 * only costs latency; the reducer drops any boundary over-delivery.
 */
import {
  EVENTS_TERMINAL_CLOSE_CODES,
  eventsSocketUrl,
  isAuthLapse,
  liveApi,
  parseLiveEventMessage,
  type LiveEvent,
} from '@/api/live'
import { ApiError } from '@/api/client'
import { ReconnectingSocket, type SocketState } from '@/lib/live/reconnectingSocket'

export interface EventsChannelOptions {
  sessionId: string
  transport: 'ws' | 'http'
  /** Highest durable seq already applied. */
  sinceSeq: () => number
  onEvents: (events: LiveEvent[]) => void
  onStateChange: (state: SocketState, attempt: number) => void
  /** The stream ended for good (not a reader any more / session gone). */
  onEnded: (reason: 'forbidden' | 'not_found' | 'finished') => void
  /** A framed 4401 already identifies a lapsed sign-in; no REST probe needed. */
  onAuthLapse?: () => void
  /** Repeated unframed failures: probe REST for auth / existence. */
  onTrouble?: () => void
  /** Server heartbeats arrive every ~10 s while the session is live. */
  expectHeartbeat: () => boolean
  createSocket?: (url: string) => WebSocket
}

export interface EventsChannel {
  readonly state: SocketState
  readonly attempt: number
  retryNow(): void
  close(): void
}

const HEARTBEAT_STALL_MS = 35_000

function isTerminalEventsClose(errorCode: string | null, closeCode: number): boolean {
  if (errorCode === 'not_found') return true
  if (errorCode === 'unauthenticated' || errorCode === 'resync') return false
  return EVENTS_TERMINAL_CLOSE_CODES.has(closeCode)
}

export function openEventsChannel(opts: EventsChannelOptions): EventsChannel {
  return opts.transport === 'ws' ? new SocketEvents(opts) : new LongPollEvents(opts)
}

class SocketEvents implements EventsChannel {
  private socket: ReconnectingSocket
  private lastMessageAt = 0
  private watchdog: ReturnType<typeof setInterval>
  private failures = 0
  private closeError: Extract<LiveEvent, { type: 'error' }> | null = null

  constructor(opts: EventsChannelOptions) {
    this.socket = new ReconnectingSocket({
      url: () => eventsSocketUrl(opts.sessionId, opts.sinceSeq()),
      isTerminalClose: (event) => isTerminalEventsClose(this.closeError?.code ?? null, event.code),
      onOpen: () => {
        this.lastMessageAt = Date.now()
        this.failures = 0
        this.closeError = null
      },
      onMessage: (event) => {
        this.lastMessageAt = Date.now()
        if (typeof event.data !== 'string') return
        const events = parseLiveEventMessage(event.data)
        for (const e of events) if (e.type === 'error') this.closeError = e
        if (events.length) opts.onEvents(events)
      },
      onClose: (event, willReconnect) => {
        const error = this.closeError
        this.closeError = null
        const reason = error?.code ?? (event.code === 4404 ? 'not_found' : event.code === 4408 ? 'resync' : event.code === 4401 ? 'unauthenticated' : null)
        if (!willReconnect) {
          opts.onEnded('not_found') // only 4404 / not_found is terminal for events WS
          return
        }
        // 4408 = server-requested resync: reconnect from the last applied seq.
        if (reason === 'resync') return
        if (reason === 'unauthenticated') {
          if (error?.code === 'unauthenticated') opts.onAuthLapse?.()
          else opts.onTrouble?.() // error frame lost: probe REST to confirm SSO
          return
        }
        this.failures += 1
        if (this.failures === 3 && !error) opts.onTrouble?.()
      },
      onStateChange: (state, attempt) => opts.onStateChange(state, attempt),
      createSocket: opts.createSocket,
    })
    this.socket.connect()
    this.watchdog = setInterval(() => {
      if (this.socket.isOpen && opts.expectHeartbeat() && Date.now() - this.lastMessageAt > HEARTBEAT_STALL_MS) {
        this.socket.kick()
      }
    }, 5_000)
  }

  get state() {
    return this.socket.state
  }

  get attempt() {
    return this.socket.attempt
  }

  retryNow() {
    this.socket.retryNow()
  }

  close() {
    clearInterval(this.watchdog)
    this.socket.close(1000, 'done')
  }
}

class LongPollEvents implements EventsChannel {
  state: SocketState = 'connecting'
  attempt = 0
  private abort = new AbortController()
  private closed = false
  private wake: (() => void) | null = null
  private opts: EventsChannelOptions
  /** Raw durable cursor (the applied seq lags by the render coalescer and
   *  skips unknown event types). */
  private cursor = 0

  constructor(opts: EventsChannelOptions) {
    this.opts = opts
    opts.onStateChange(this.state, 0)
    void this.loop()
  }

  retryNow() {
    this.wake?.()
  }

  close() {
    this.closed = true
    this.abort.abort()
    this.wake?.()
    this.setState('closed')
  }

  private setState(state: SocketState) {
    if (state === this.state) return
    this.state = state
    this.opts.onStateChange(state, this.attempt)
  }

  private async loop() {
    while (!this.closed) {
      try {
        const since = Math.max(this.cursor, this.opts.sinceSeq())
        const res = await liveApi.pollEvents(this.opts.sessionId, since, this.abort.signal)
        if (this.closed) return
        if (res.cursor !== null) this.cursor = Math.max(since, res.cursor)
        this.attempt = 0
        this.setState('open')
        const events: LiveEvent[] = [...res.events]
        // The tail is outside the durable list here; feed it as the
        // ephemeral event the socket would have sent.
        if (res.interim) events.push({ type: 'segment.interim', seq: null, ts: null, interim: res.interim })
        if (events.length) this.opts.onEvents(events)
        const terminal = res.status === 'finalized' || res.status === 'cancelled' || res.status === 'failed'
        if (terminal && res.events.length === 0) {
          this.opts.onEnded('finished')
          this.setState('closed')
          return
        }
      } catch (error) {
        if (this.closed) return
        const status = error instanceof ApiError ? error.status : 0
        if (status === 403 || status === 404) {
          this.opts.onEnded(status === 403 ? 'forbidden' : 'not_found')
          this.setState('closed')
          return
        }
        this.attempt += 1
        this.setState('reconnecting')
        if (this.attempt === 3 || isAuthLapse(error)) this.opts.onTrouble?.()
        const delay = Math.min(10_000, 500 * 2 ** Math.min(this.attempt - 1, 10)) * (0.75 + Math.random() * 0.5)
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, delay)
          this.wake = () => {
            clearTimeout(timer)
            resolve()
          }
        })
        this.wake = null
      }
    }
  }
}
