/**
 * Ordered, resumable audio uplink (`README.md` §2).
 *
 * All audio is addressed by absolute sample index in the server's
 * coordinate space. The buffer keeps everything the server has not
 * acknowledged; after any drop the server names the sample it holds
 * (`ready.received_samples`, or `received_samples` on an HTTP gap 409) and we
 * resume exactly there — no gaps, no duplicates — as long as that audio is
 * still buffered. The buffer is capped well above the contract's
 * `client_buffer_s`; past the cap the oldest unacked audio is dropped,
 * counted, and the stream is rebased so it stays contiguous.
 *
 * Two transports share the buffer: the audio WebSocket (default) and the
 * `POST /audio?offset_samples=N` fallback when `config.websockets` is false.
 */
import {
  AUDIO_CLOSE,
  AUDIO_TERMINAL_CLOSE_CODES,
  BYTES_PER_SAMPLE,
  HTTP_AUDIO_MAX_BYTES,
  audioSocketUrl,
  isAuthLapse,
  liveApi,
  parseAudioServerMessage,
  type LiveAudioFormat,
} from '@/api/live'
import { ApiError } from '@/api/client'
import { ReconnectingSocket, type SocketState } from '@/lib/live/reconnectingSocket'

// ---------------------------------------------------------------------------
// Sample-indexed buffer (pure; unit-tested)
// ---------------------------------------------------------------------------

interface Chunk {
  start: number
  bytes: Uint8Array
}

export class SampleBuffer {
  private chunks: Chunk[] = []
  /** Next sample index to be captured (end of buffered audio). */
  end = 0
  /** Next sample index to send. */
  cursor = 0
  /** Highest sample index the server holds durably. */
  acked = 0
  /** Unacked samples discarded by the cap. */
  droppedSamples = 0
  private maxSamples: number

  constructor(maxBytes: number) {
    this.maxSamples = Math.floor(maxBytes / BYTES_PER_SAMPLE)
  }

  get start(): number {
    return this.chunks.length ? this.chunks[0].start : this.end
  }

  get unsentSamples(): number {
    return this.end - this.cursor
  }

  get unackedSamples(): number {
    return this.end - this.acked
  }

  push(pcm: ArrayBuffer): void {
    const bytes = new Uint8Array(pcm)
    if (bytes.byteLength < BYTES_PER_SAMPLE) return
    const even = bytes.byteLength - (bytes.byteLength % BYTES_PER_SAMPLE)
    this.chunks.push({ start: this.end, bytes: even === bytes.byteLength ? bytes : bytes.subarray(0, even) })
    this.end += even / BYTES_PER_SAMPLE
    this.enforceCap()
  }

  /** Server confirmed it holds everything before `samples`. */
  ack(samples: number): void {
    if (samples <= this.acked) return
    this.acked = Math.min(samples, this.end)
    if (this.cursor < this.acked) this.cursor = this.acked
    this.release(this.acked)
  }

  /** Server says it holds exactly `samples` — continue from there. */
  resumeFrom(samples: number): void {
    if (samples < this.start) {
      // Audio the server never got was dropped by the cap: rebase so the
      // stream stays contiguous (the gap is already counted as dropped).
      this.shift(samples - this.start)
    } else if (samples > this.end) {
      // Server is ahead of us (another uplink fed it): nothing buffered is
      // still useful; continue numbering from the server's position.
      this.chunks = []
      this.shift(samples - this.end)
    }
    this.acked = samples
    this.cursor = samples
    this.release(samples)
  }

  /** Up to `maxBytes` of contiguous audio from the cursor (without
   *  advancing it), or null when nothing is waiting. */
  peek(maxBytes: number): { offset: number; data: ArrayBuffer } | null {
    if (this.cursor < this.start) this.cursor = this.start
    if (this.cursor >= this.end) return null
    const limit = maxBytes - (maxBytes % BYTES_PER_SAMPLE)
    const parts: Uint8Array[] = []
    let total = 0
    for (const chunk of this.chunks) {
      if (total >= limit) break
      const chunkEnd = chunk.start + chunk.bytes.byteLength / BYTES_PER_SAMPLE
      if (chunkEnd <= this.cursor) continue
      let slice = chunk.bytes.subarray(Math.max(0, this.cursor - chunk.start) * BYTES_PER_SAMPLE)
      if (slice.byteLength > limit - total) slice = slice.subarray(0, limit - total)
      parts.push(slice)
      total += slice.byteLength
    }
    if (total === 0) return null
    const out = new Uint8Array(total)
    let at = 0
    for (const p of parts) {
      out.set(p, at)
      at += p.byteLength
    }
    return { offset: this.cursor, data: out.buffer }
  }

  advance(bytes: number): void {
    this.cursor = Math.min(this.end, this.cursor + bytes / BYTES_PER_SAMPLE)
  }

  private release(before: number): void {
    while (this.chunks.length) {
      const c = this.chunks[0]
      const chunkEnd = c.start + c.bytes.byteLength / BYTES_PER_SAMPLE
      if (chunkEnd <= before) this.chunks.shift()
      else {
        if (c.start < before) this.chunks[0] = { start: before, bytes: c.bytes.subarray((before - c.start) * BYTES_PER_SAMPLE) }
        break
      }
    }
  }

  private shift(delta: number): void {
    if (delta === 0) return
    this.chunks = this.chunks.map((c) => ({ start: c.start + delta, bytes: c.bytes }))
    this.end += delta
    this.cursor += delta
    this.acked += delta
  }

  private enforceCap(): void {
    while (this.chunks.length > 1 && this.end - this.start > this.maxSamples) {
      const c = this.chunks.shift()!
      const n = c.bytes.byteLength / BYTES_PER_SAMPLE
      const unacked = Math.max(0, c.start + n - Math.max(this.acked, c.start))
      this.droppedSamples += unacked
    }
  }
}

// ---------------------------------------------------------------------------
// Uplink
// ---------------------------------------------------------------------------

export type UplinkEndReason = 'stopped' | 'superseded' | 'not_owner' | 'not_found' | 'bad_frame' | 'unavailable'

export interface UplinkCloseContext {
  closeCode: number
  hasErrorFrame: boolean
}

export interface UplinkStats {
  state: SocketState
  attempt: number
  /** Audio captured but not yet handed to the network, ms. */
  bufferedMs: number
  /** Audio not yet durably stored on the server, ms. */
  unackedMs: number
  /** Audio discarded because the backlog overflowed, ms. */
  droppedMs: number
  serverError: string | null
  /** Sign-in lapsed; audio keeps buffering until it's restored. */
  authLapsed: boolean
  /** Why the uplink ended for good (null while it's running). */
  ended: UplinkEndReason | null
}

export interface AudioUplinkOptions {
  sessionId: string
  format: LiveAudioFormat
  transport: 'ws' | 'http'
  maxBufferBytes: number
  onChange: () => void
  onEnded?: (reason: UplinkEndReason, message: string | null, context?: UplinkCloseContext) => void
  createSocket?: (url: string) => WebSocket
}

const SOCKET_HIGH_WATER = 128 * 1024
/** No server message (acks arrive ~1/s) for this long while audio waits for
 *  an ack → treat the socket as dead and reconnect (the resume is exact). */
export const ACK_STALL_MS = 5_000
/** Quiet this long with unacked audio → ping, so a live socket answers
 *  before the stall deadline even when capture paused right after a send. */
const PING_AFTER_MS = 2_000
/** A handshake that hangs this long (proxy, captive network) is abandoned. */
const CONNECT_TIMEOUT_MS = 10_000

export class AudioUplink {
  readonly buffer: SampleBuffer
  private opts: AudioUplinkOptions
  private socket: ReconnectingSocket | null = null
  private ready = false
  private lastServerAt = 0
  private lastPingAt = 0
  private serverError: string | null = null
  /** Error frames belong to one connection only, not its reconnect attempts. */
  private closeError: Extract<NonNullable<ReturnType<typeof parseAudioServerMessage>>, { type: 'error' }> | null = null
  private authLapsed = false
  private ended: UplinkEndReason | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private closed = false
  // http transport
  private httpState: SocketState = 'idle'
  private httpAttempt = 0
  private httpInflight = false
  private httpRetryAt = 0
  private draining = false
  private started = false

  constructor(opts: AudioUplinkOptions) {
    this.opts = opts
    this.buffer = new SampleBuffer(opts.maxBufferBytes)
  }

  /** Buffer audio from the first frame; `start()` opens the transport. */
  push(frame: ArrayBuffer): void {
    if (this.closed) return
    this.buffer.push(frame)
    if (this.started) this.pump()
  }

  start(): void {
    if (this.started || this.closed) return
    this.started = true
    if (this.opts.transport === 'ws') this.startSocket()
    else this.httpState = 'connecting'
    this.timer = setInterval(() => this.onTimer(), 250)
    this.pump()
  }

  get stats(): UplinkStats {
    const rate = this.opts.format.sampleRate / 1000
    const inSocket = (this.socket?.bufferedAmount ?? 0) / BYTES_PER_SAMPLE
    return {
      state: this.opts.transport === 'ws' ? (this.socket?.state ?? 'idle') : this.httpState,
      attempt: this.opts.transport === 'ws' ? (this.socket?.attempt ?? 0) : this.httpAttempt,
      bufferedMs: Math.round((this.buffer.unsentSamples + inSocket) / rate),
      unackedMs: Math.round(this.buffer.unackedSamples / rate),
      droppedMs: Math.round(this.buffer.droppedSamples / rate),
      serverError: this.serverError,
      authLapsed: this.authLapsed,
      ended: this.ended,
    }
  }

  retryNow(): void {
    this.socket?.retryNow()
    this.httpRetryAt = 0
    this.pump()
  }

  /** Resolve true once the server has durably stored everything captured
   *  (exact, via acks); false on timeout or if the uplink ended. */
  async drain(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs
    this.draining = true
    for (;;) {
      this.pump()
      if (this.buffer.acked >= this.buffer.end) return true
      if (this.ended || Date.now() >= deadline) return false
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    this.socket?.close(1000, 'capture stopped')
    this.socket = null
    if (this.httpState !== 'closed') this.httpState = 'closed'
    this.opts.onChange()
  }

  // --- WebSocket transport ----------------------------------------------------

  private startSocket(): void {
    this.socket = new ReconnectingSocket({
      url: () => audioSocketUrl(this.opts.sessionId),
      binaryType: 'arraybuffer',
      createSocket: this.opts.createSocket,
      // Prefer the server's machine-readable error over a close frame;
      // 4401 retries for re-auth, and 1011 reconnects from the next ready.
      isTerminalClose: (event) => isTerminalAudioClose(this.closeError?.code ?? null, event.code),
      maxDelayMs: 10_000,
      onOpen: () => {
        // Nothing may be sent until `ready` names the resume point.
        this.ready = false
        this.closeError = null
        this.serverError = null
        this.lastServerAt = Date.now()
        this.opts.onChange()
      },
      onMessage: (event) => {
        if (typeof event.data !== 'string') return
        const msg = parseAudioServerMessage(event.data)
        if (!msg) return
        this.lastServerAt = Date.now()
        if (msg.type === 'ready') {
          this.buffer.resumeFrom(msg.receivedSamples)
          this.ready = true
          this.authLapsed = false
          this.serverError = null
          this.pump()
          this.opts.onChange()
        } else if (msg.type === 'ack') {
          this.buffer.ack(msg.receivedSamples)
          this.opts.onChange()
        } else if (msg.type === 'error') {
          this.closeError = msg
          this.serverError = msg.message
          this.opts.onChange()
        }
      },
      onClose: (event, willReconnect) => {
        const error = this.closeError
        this.closeError = null
        this.ready = false
        if ((error ? error.code === 'unauthenticated' : event.code === AUDIO_CLOSE.unauthenticated)) this.authLapsed = true
        if (!willReconnect && !this.closed) {
          this.end(closeReason(error?.code ?? null, event.code), error?.message ?? this.serverError, {
            closeCode: event.code,
            hasErrorFrame: error !== null,
          })
        }
        this.opts.onChange()
      },
      onStateChange: () => this.opts.onChange(),
    })
    this.socket.connect()
  }

  private pumpSocket(): void {
    const socket = this.socket
    if (!socket || !socket.isOpen || !this.ready) return
    const max = Math.min(this.opts.format.maxFrameBytes, HTTP_AUDIO_MAX_BYTES)
    while (socket.bufferedAmount < SOCKET_HIGH_WATER) {
      const next = this.buffer.peek(max)
      if (!next) break
      if (!socket.send(next.data)) break
      this.buffer.advance(next.data.byteLength)
    }
  }

  // --- HTTP fallback transport -------------------------------------------------

  private async pumpHttp(): Promise<void> {
    if (this.httpInflight || this.closed || this.ended) return
    if (Date.now() < this.httpRetryAt) return
    // Batch ~1 s per request in steady state; flush everything on drain.
    if (!this.draining && this.buffer.unsentSamples < this.opts.format.sampleRate) return
    const next = this.buffer.peek(HTTP_AUDIO_MAX_BYTES)
    if (!next) return
    this.httpInflight = true
    try {
      const res = await liveApi.sendAudio(this.opts.sessionId, next.offset, next.data)
      if (res.mismatch) this.buffer.resumeFrom(res.receivedSamples)
      else this.buffer.ack(res.receivedSamples)
      this.httpState = 'open'
      this.httpAttempt = 0
      this.authLapsed = false
    } catch (error) {
      const status = error instanceof ApiError ? error.status : 0
      if (status === 403 || status === 404 || status === 409) {
        this.end(status === 403 ? 'not_owner' : status === 404 ? 'not_found' : 'stopped', error instanceof Error ? error.message : null)
      } else {
        if (isAuthLapse(error)) this.authLapsed = true
        this.httpAttempt += 1
        this.httpState = 'reconnecting'
        const base = Math.min(10_000, 500 * 2 ** Math.min(this.httpAttempt - 1, 10))
        this.httpRetryAt = Date.now() + Math.round(base * (0.75 + Math.random() * 0.5))
      }
    } finally {
      this.httpInflight = false
      this.opts.onChange()
    }
  }

  // --- shared ----------------------------------------------------------------

  private pump(): void {
    if (!this.started || this.closed) return
    if (this.opts.transport === 'ws') this.pumpSocket()
    else void this.pumpHttp()
  }

  private onTimer(): void {
    if (this.opts.transport === 'http') {
      void this.pumpHttp()
      return
    }
    const socket = this.socket
    const now = Date.now()
    if (socket && socket.connectingMs > CONNECT_TIMEOUT_MS) {
      socket.kick()
      return
    }
    if (socket?.isOpen && this.buffer.unackedSamples > 0) {
      const quiet = now - this.lastServerAt
      if (quiet > ACK_STALL_MS) {
        socket.kick()
        return
      }
      if (this.ready && quiet > PING_AFTER_MS && now - this.lastPingAt > PING_AFTER_MS) {
        this.lastPingAt = now
        socket.send(JSON.stringify({ type: 'ping' }))
      }
    }
    this.pump()
  }

  private end(reason: UplinkEndReason, message: string | null, context?: UplinkCloseContext): void {
    if (this.ended) return
    this.ended = reason
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    this.httpState = 'closed'
    this.opts.onEnded?.(reason, message, context)
  }
}

const TERMINAL_AUDIO_ERRORS: ReadonlySet<string> = new Set([
  'bad_frame', 'forbidden', 'not_found', 'not_live', 'superseded', 'unavailable',
])

function isTerminalAudioClose(errorCode: string | null, closeCode: number): boolean {
  if (errorCode === 'unauthenticated' || errorCode === 'server_error') return false
  if (errorCode && TERMINAL_AUDIO_ERRORS.has(errorCode)) return true
  return AUDIO_TERMINAL_CLOSE_CODES.has(closeCode)
}

function closeReason(errorCode: string | null, closeCode: number): UplinkEndReason {
  switch (errorCode) {
    case 'superseded': return 'superseded'
    case 'forbidden': return 'not_owner'
    case 'not_found': return 'not_found'
    case 'bad_frame': return 'bad_frame'
    case 'unavailable': return 'unavailable'
    case 'not_live': return 'stopped'
  }
  switch (closeCode) {
    case AUDIO_CLOSE.superseded: return 'superseded'
    case AUDIO_CLOSE.notOwner: return 'not_owner'
    case AUDIO_CLOSE.notFound: return 'not_found'
    case AUDIO_CLOSE.badFrame: return 'bad_frame'
    case AUDIO_CLOSE.unavailable: return 'unavailable'
    default: return 'stopped'
  }
}
