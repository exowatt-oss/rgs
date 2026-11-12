/** Minimal controllable WebSocket for Live Record tests (jsdom has none
 *  that can be driven from a test). Install with
 *  `vi.stubGlobal('WebSocket', FakeWebSocket)`. */
export class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSING = 2
  readonly CLOSED = 3

  url: string
  readyState = 0
  binaryType: BinaryType = 'blob'
  bufferedAmount = 0
  sent: (string | ArrayBuffer)[] = []
  closedWith: { code?: number; reason?: string } | null = null
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  static reset(): void {
    FakeWebSocket.instances = []
  }

  static last(match?: string): FakeWebSocket {
    const list = match ? FakeWebSocket.instances.filter((s) => s.url.includes(match)) : FakeWebSocket.instances
    const socket = list[list.length - 1]
    if (!socket) throw new Error(`no FakeWebSocket${match ? ` matching ${match}` : ''}`)
    return socket
  }

  send(data: string | ArrayBuffer): void {
    if (this.readyState !== 1) throw new Error('send on a socket that is not open')
    this.sent.push(data)
  }

  close(code?: number, reason?: string): void {
    this.closedWith = { code, reason }
    this.readyState = 3
  }

  // --- test drivers ---

  open(): void {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }

  receive(data: unknown): void {
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    this.onmessage?.({ data: payload } as MessageEvent)
  }

  serverClose(code: number, reason = ''): void {
    this.readyState = 3
    this.onclose?.({ code, reason, wasClean: code === 1000 } as CloseEvent)
  }

  get binarySent(): ArrayBuffer[] {
    return this.sent.filter((d): d is ArrayBuffer => d instanceof ArrayBuffer)
  }
}
