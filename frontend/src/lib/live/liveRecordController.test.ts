import { afterEach, describe, expect, it, vi } from 'vitest'

import { liveApi, normalizeSession } from '@/api/live'
import { LiveRecordController } from '@/lib/live/liveRecordController'
import type { UplinkCloseContext, UplinkEndReason } from '@/lib/live/audioUplink'

function capturing() {
  const controller = new LiveRecordController()
  controller.getSnapshot().session = normalizeSession({ id: 'ses-1', status: 'live', is_owner: true })
  controller.getSnapshot().phase = 'live'
  const internal = controller as unknown as {
    recording: boolean
    onUplinkEnded: (reason: UplinkEndReason, message: string | null, context?: UplinkCloseContext) => Promise<void>
  }
  internal.recording = true
  return { controller, internal }
}

afterEach(() => vi.restoreAllMocks())

describe('LiveRecordController audio-close diagnosis', () => {
  it.each([
    ['stopped', 4409, 'ended'],
    ['superseded', 4410, 'viewing'],
  ] as const)('uses framed %s directly without fetching session status', async (reason, closeCode, phase) => {
    const { controller, internal } = capturing()
    const get = vi.spyOn(liveApi, 'get')
    await internal.onUplinkEnded(reason, 'server error', { closeCode, hasErrorFrame: true })
    expect(get).not.toHaveBeenCalled()
    expect(controller.getSnapshot().phase).toBe(phase)
    if (reason === 'superseded') expect(controller.getSnapshot().warning).toMatch(/another tab or device/)
  })

  it.each([4409, 4410])('falls back to a status check on %i without an error frame', async (closeCode) => {
    const { controller, internal } = capturing()
    const get = vi.spyOn(liveApi, 'get').mockResolvedValue(normalizeSession({ id: 'ses-1', status: 'stopping' }))
    await internal.onUplinkEnded(closeCode === 4410 ? 'superseded' : 'stopped', null, { closeCode, hasErrorFrame: false })
    expect(get).toHaveBeenCalledExactlyOnceWith('ses-1')
    expect(controller.getSnapshot().session?.status).toBe('stopping')
    expect(controller.getSnapshot().phase).toBe('ended')
  })

  it('does not probe on an ordinary 1000 stop (which has no error frame by design)', async () => {
    const { controller, internal } = capturing()
    const get = vi.spyOn(liveApi, 'get')
    await internal.onUplinkEnded('stopped', null, { closeCode: 1000, hasErrorFrame: false })
    expect(get).not.toHaveBeenCalled()
    expect(controller.getSnapshot().phase).toBe('ended')
  })
})

describe('LiveRecordController mic health', () => {
  function withFakeCapture() {
    const controller = new LiveRecordController()
    const capture = {
      context: { state: 'running' },
      micLabel: 'MacBook Pro Microphone',
      micTrackState: 'live' as 'live' | 'muted' | 'ended' | null,
      resume: vi.fn(async () => {}),
      setMic: vi.fn(async () => {}),
      close: vi.fn(async () => {}),
    }
    const internal = controller as unknown as {
      capture: typeof capture
      health: { level: (rms: number, now: number) => void } | null
      armHealth: (c: typeof capture) => void
    }
    internal.capture = capture
    controller.getSnapshot().micLabel = capture.micLabel
    internal.armHealth(capture)
    return { controller, capture, internal }
  }

  afterEach(() => {
    vi.useRealTimers()
  })

  it('raises "no sound" on digital zeros and clears it after Retry mic', async () => {
    vi.useFakeTimers()
    const { controller, capture, internal } = withFakeCapture()
    for (let i = 0; i < 70; i += 1) {
      internal.health?.level(0, Date.now())
      vi.advanceTimersByTime(50)
    }
    expect(controller.getSnapshot().micProblem).toBe('silent')

    await controller.retryMic()
    expect(capture.resume).toHaveBeenCalled()
    expect(capture.setMic).toHaveBeenCalledOnce()
    expect(controller.getSnapshot().micProblem).toBeNull()
    controller.dispose()
  })

  it('resumes the context and re-acquires a muted mic when the tab becomes visible', async () => {
    const { controller, capture } = withFakeCapture()
    capture.micTrackState = 'muted'
    controller.onVisible()
    await vi.waitFor(() => expect(capture.setMic).toHaveBeenCalledOnce())
    expect(capture.resume).toHaveBeenCalled()
    controller.dispose()
  })
})

describe('LiveRecordController start vs. Home unmount', () => {
  it('stopPreview does not close the capture while a start is in flight', async () => {
    const controller = new LiveRecordController()
    const close = vi.fn(async () => {})
    const internal = controller as unknown as { capture: { close: () => Promise<void> } | null; recording: boolean }
    internal.capture = { close }
    controller.getSnapshot().phase = 'starting'
    await controller.stopPreview()
    expect(close).not.toHaveBeenCalled()
    controller.getSnapshot().phase = 'idle'
    await controller.stopPreview()
    expect(close).toHaveBeenCalledOnce()
  })
})
