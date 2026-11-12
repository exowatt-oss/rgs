/**
 * A WebSocket that reconnects with capped, jittered exponential backoff.
 *
 * Used for both live-session sockets. The URL is resolved on every attempt
 * so the events socket can resume with the latest `since_seq`. Terminal
 * close codes (session gone, forbidden, replaced) stop the retry loop.
 */
export type SocketState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface ReconnectingSocketOptions {
  url: () => string
  binaryType?: BinaryType
  onOpen?: (socket: WebSocket) => void
  onMessage?: (event: MessageEvent) => void
  /** `willReconnect` is false for intentional and terminal closes. */
  onClose?: (event: CloseEvent, willReconnect: boolean) => void
  onStateChange?: (state: SocketState, attempt: number) => void
  isTerminalClose?: (event: CloseEvent) => boolean
  createSocket?: (url: string) => WebSocket
  minDelayMs?: number
  maxDelayMs?: number
  /** An open that lasts at least this long resets the backoff. */
  stableAfterMs?: number
}

export class ReconnectingSocket {
  state: SocketState = 'idle'
  /** Consecutive failed attempts since the last stable connection. */
  attempt = 0
  socket: WebSocket | null = null

  private opts: ReconnectingSocketOptions
  private timer: ReturnType<typeof setTimeout> | null = null
  private openedAt = 0
  private connectStartedAt = 0
  private intentionallyClosed = false
  private readonly onOnline = () => this.retryNow()

  constructor(opts: ReconnectingSocketOptions) {
    this.opts = opts
  }

  connect(): void {
    if (this.intentionallyClosed || this.socket) return
    this.clearTimer()
    if (typeof window !== 'undefined') window.addEventListener('online', this.onOnline)
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting')
    let socket: WebSocket
    try {
      const url = this.opts.url()
      socket = this.opts.createSocket ? this.opts.createSocket(url) : new WebSocket(url)
    } catch {
      this.scheduleReconnect()
      return
    }
    if (this.opts.binaryType) socket.binaryType = this.opts.binaryType
    this.socket = socket
    this.connectStartedAt = Date.now()
    socket.onopen = () => {
      if (this.socket !== socket) return
      this.openedAt = Date.now()
      this.setState('open')
      this.opts.onOpen?.(socket)
    }
    socket.onmessage = (event) => {
      if (this.socket === socket) this.opts.onMessage?.(event)
    }
    socket.onerror = () => {
      // `close` always follows `error`; reconnect logic lives there.
    }
    socket.onclose = (event) => {
      if (this.socket !== socket) return
      this.socket = null
      const wasStable = this.openedAt > 0 && Date.now() - this.openedAt >= (this.opts.stableAfterMs ?? 5_000)
      this.openedAt = 0
      if (wasStable) this.attempt = 0
      if (this.intentionallyClosed || this.opts.isTerminalClose?.(event)) {
        this.intentionallyClosed = true
        this.teardown()
        this.setState('closed')
        this.opts.onClose?.(event, false)
        return
      }
      this.opts.onClose?.(event, true)
      this.scheduleReconnect()
    }
  }

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): boolean {
    const socket = this.socket
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    socket.send(data as ArrayBuffer)
    return true
  }

  get bufferedAmount(): number {
    return this.socket?.bufferedAmount ?? 0
  }

  /** How long the current handshake has been pending (0 when none is). */
  get connectingMs(): number {
    return this.socket?.readyState === WebSocket.CONNECTING ? Date.now() - this.connectStartedAt : 0
  }

  get isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN
  }

  /** Drop a connection that looks dead (no traffic past a heartbeat
   *  window, e.g. a half-open TCP socket after laptop sleep) and reconnect
   *  at once instead of waiting for the browser's own close timeout. */
  kick(): void {
    const socket = this.socket
    if (this.intentionallyClosed || !socket) return
    this.socket = null
    this.openedAt = 0
    socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null
    try {
      socket.close(4000, 'stale connection')
    } catch {
      // ignore
    }
    this.opts.onClose?.({ code: 4000, reason: 'stale connection', wasClean: false } as CloseEvent, true)
    this.clearTimer()
    this.attempt += 1
    this.connect()
  }

  /** Skip the backoff wait (network came back, tab became visible). */
  retryNow(): void {
    if (this.intentionallyClosed || this.socket || this.state !== 'reconnecting') return
    this.clearTimer()
    this.connect()
  }

  close(code = 1000, reason = 'client closed'): void {
    this.intentionallyClosed = true
    this.clearTimer()
    const socket = this.socket
    this.socket = null
    this.teardown()
    if (socket && socket.readyState < WebSocket.CLOSING) {
      try {
        socket.close(code, reason)
      } catch {
        // Already closing — nothing to do.
      }
    }
    this.setState('closed')
  }

  private scheduleReconnect(): void {
    this.attempt += 1
    this.setState('reconnecting')
    const min = this.opts.minDelayMs ?? 500
    const max = this.opts.maxDelayMs ?? 10_000
    const base = Math.min(max, min * 2 ** Math.min(this.attempt - 1, 10))
    const delay = Math.round(base * (0.75 + Math.random() * 0.5))
    this.clearTimer()
    this.timer = setTimeout(() => {
      this.timer = null
      this.connect()
    }, delay)
  }

  private teardown(): void {
    if (typeof window !== 'undefined') window.removeEventListener('online', this.onOnline)
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private setState(state: SocketState): void {
    this.state = state
    this.opts.onStateChange?.(state, this.attempt)
  }
}
