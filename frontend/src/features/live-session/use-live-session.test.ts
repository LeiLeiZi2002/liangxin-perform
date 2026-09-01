import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LiveAudioCaptureError } from '../live-audio/audio-capture'
import { useLiveSession, type LiveSessionDependencies } from './use-live-session'

class FakeSocket {
  static readonly OPEN = 1
  readyState = FakeSocket.OPEN
  binaryType = ''
  sent: Array<string | ArrayBuffer> = []
  closeCalls: Array<{ code?: number; reason?: string }> = []
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  send(data: string | ArrayBuffer) { this.sent.push(data) }
  close(code?: number, reason?: string) {
    this.closeCalls.push({ code, reason })
    this.readyState = 3
  }
  serverClose(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code } as CloseEvent)
  }
  open() { this.onopen?.() }
  json(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent) }
  binary(data = new Uint8Array([0, 0]).buffer) {
    this.onmessage?.({ data } as MessageEvent)
  }
}

function setup(
  media: 'voice' | 'text' = 'voice',
  overrides: Partial<LiveSessionDependencies> = {},
) {
  const sockets: FakeSocket[] = []
  const capture = { start: vi.fn().mockResolvedValue(undefined), close: vi.fn() }
  let captureCallbacks: Parameters<NonNullable<LiveSessionDependencies['createCapture']>>[0]
  let playbackIdle: (() => void) | undefined
  const playback = {
    queue: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
    close: vi.fn().mockResolvedValue(undefined),
  }
  const createPlayback = vi.fn((onIdle: () => void) => {
    playbackIdle = onIdle
    return playback
  })
  const dependencies: LiveSessionDependencies = {
    createSocket: () => {
      const socket = new FakeSocket()
      sockets.push(socket)
      return socket as unknown as WebSocket
    },
    createCapture: (callbacks) => {
      captureCallbacks = callbacks
      return capture
    },
    createPlayback,
    reconnectDelayMs: 10,
    ...overrides,
  }
  const hook = renderHook(
    ({ sessionId }: { sessionId: string }) => useLiveSession(sessionId, media, dependencies),
    { initialProps: { sessionId: 'session-1' } },
  )
  return {
    ...hook,
    sockets,
    capture,
    playback,
    createPlayback,
    getCaptureCallbacks: () => captureCallbacks,
    finishPlayback: () => playbackIdle?.(),
  }
}

function sentJson(socket: FakeSocket) {
  return socket.sent
    .filter((item): item is string => typeof item === 'string')
    .map((item) => JSON.parse(item) as { type: string; [key: string]: unknown })
}

function endedSessionResponse(endReason: string, id = 'session-1') {
  return new Response(JSON.stringify({
    id, mode: 'assessment', scene: 'hotline', case_type: 'main',
    case_id: 'case-1', media: 'voice', status: 'ended', model_mode: 'live',
    soft_duration_minutes: 15, created_at: 'now', updated_at: 'now', ended_at: 'now',
    end_reason: endReason,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

describe('useLiveSession', () => {
  afterEach(() => vi.useRealTimers())

  it('用 snapshot 恢复完整原文，技术暂停时保留原文并允许重试', async () => {
    const live = setup('voice')
    const socket = live.sockets[0]

    act(() => socket.open())
    act(() => socket.json({
      type: 'snapshot', media: 'voice', phase: 'listening',
      transcript: [{ id: 'c1', sequence: 1, speaker: 'client', text: '喂，你好。', client_turn_id: 'opening' }],
    }))
    expect(live.result.current.transcript.map((turn) => turn.text)).toEqual(['喂，你好。'])

    act(() => socket.json({
      type: 'technical.pause', phase: 'technical_paused',
      message: '来访者的信号不太稳定', can_retry: true,
    }))
    expect(live.result.current.technicalPause?.message).toBe('来访者的信号不太稳定')
    expect(live.result.current.transcript.map((turn) => turn.text)).toEqual(['喂，你好。'])

    act(() => live.result.current.retry())
    expect(sentJson(socket).at(-1)).toEqual({ type: 'technical.retry' })
    expect(live.result.current.technicalPause?.message).toBe('正在重新接通…')
    expect(live.result.current.retrying).toBe(true)

    const pcm = new Int16Array(320).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(pcm))
    expect(socket.sent).not.toContain(pcm)

    act(() => socket.json({ type: 'phase', phase: 'listening' }))
    expect(live.result.current.technicalPause).toBeNull()
    expect(live.result.current.retrying).toBe(false)
    act(() => live.getCaptureCallbacks().onPcmFrame(pcm))
    expect(socket.sent).toContain(pcm)
  })

  it('snapshot 按服务端的 can_retry 恢复不可重试状态', () => {
    const live = setup('voice')
    const socket = live.sockets[0]

    act(() => socket.open())
    act(() => socket.json({
      type: 'snapshot', media: 'voice', phase: 'technical_paused',
      transcript: [], can_retry: false,
    }))

    expect(live.result.current.technicalPause).toEqual({
      message: '来访者的信号不太稳定', canRetry: false,
    })
  })

  it('收到不可重试的技术暂停时立即释放采集与播放资源', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => socket.json({
      type: 'technical.pause', phase: 'technical_paused',
      message: '本次连接无法恢复', can_retry: false,
    }))

    expect(live.capture.close).toHaveBeenCalled()
    expect(live.playback.stop).toHaveBeenCalled()
    expect(live.playback.close).toHaveBeenCalled()
  })

  it('可重试的技术暂停保留媒体资源供原连接恢复', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => socket.json({
      type: 'technical.pause', phase: 'technical_paused',
      message: '来访者的信号不太稳定', can_retry: true,
    }))

    expect(live.capture.close).not.toHaveBeenCalled()
    expect(live.playback.close).not.toHaveBeenCalled()
  })

  it('展示实时转写，并将确认回合合并进原文', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({ type: 'asr.partial', transcript: '我想先问一下', text: '我想先问一下' }))
    expect(live.result.current.liveTranscript).toBe('我想先问一下')

    act(() => socket.json({
      type: 'turn.committed', client_turn_id: 'voice-1',
      worker: { id: 'w1', sequence: 1, speaker: 'worker', text: '我想先问一下。', client_turn_id: 'voice-1' },
      client: { id: 'c2', sequence: 2, speaker: 'client', text: '嗯，你问吧。', client_turn_id: 'voice-1' },
    }))
    expect(live.result.current.liveTranscript).toBe('')
    expect(live.result.current.transcript.map((turn) => turn.text)).toEqual([
      '我想先问一下。', '嗯，你问吧。',
    ])
  })

  it('识别不对时清空本轮并等待服务端确认后再接收新语音', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({
      type: 'asr.partial', transcript: '关系的，没有没有关系的。',
    }))

    let requested = false
    act(() => { requested = live.result.current.redoInput() })

    expect(requested).toBe(true)
    expect(sentJson(socket).at(-1)).toEqual({ type: 'turn.redo_input' })
    expect(live.result.current.redoInputPending).toBe(true)
    expect(live.result.current.canRedoInput).toBe(false)
    const pcmDuringReset = new Int16Array([10]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(pcmDuringReset))
    expect(socket.sent).not.toContain(pcmDuringReset)

    act(() => socket.json({
      type: 'input.reset', message: '已清空，请重新说这一句',
    }))
    expect(live.result.current.liveTranscript).toBe('')
    expect(live.result.current.redoInputPending).toBe(false)
    expect(live.result.current.inputNotice).toBe('已清空，请重新说这一句')
    const freshPcm = new Int16Array([11]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(freshPcm))
    expect(socket.sent).toContain(freshPcm)

    act(() => socket.json({ type: 'asr.final', transcript: '没关系的。' }))
    expect(live.result.current.liveTranscript).toBe('没关系的。')
    expect(live.result.current.inputNotice).toBe('')
  })

  it('snapshot 明确不支持重说时，不允许发送重说事件', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({
      type: 'snapshot', media: 'voice', phase: 'listening', transcript: [], can_redo_input: false,
    }))
    act(() => socket.json({ type: 'asr.partial', transcript: '我想再确认一下。' }))

    expect(live.result.current.canRedoInput).toBe(false)
    expect(live.result.current.redoInput()).toBe(false)
    expect(sentJson(socket).some((event) => event.type === 'turn.redo_input')).toBe(false)
  })

  it('重说时识别连接失败也会清掉已作废文字并解除等待状态', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({ type: 'asr.final', transcript: '这句识别错了' }))
    act(() => live.result.current.redoInput())

    act(() => socket.json({
      type: 'technical.pause',
      phase: 'technical_paused',
      message: '来访者的信号不太稳定',
      can_retry: true,
    }))

    expect(live.result.current.liveTranscript).toBe('')
    expect(live.result.current.redoInputPending).toBe(false)
    expect(live.result.current.technicalPause?.message).toBe('来访者的信号不太稳定')
  })

  it('音频分片自然播放完成后才发送 playback.ended', async () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({ type: 'visitor.text', text: '我在听。', provisional: true }))
    act(() => socket.binary(new Uint8Array([0, 0, 1, 0]).buffer))
    await waitFor(() => expect(live.playback.queue).toHaveBeenCalledOnce())
    act(() => socket.json({
      type: 'turn.committed', client_turn_id: 'voice-1',
      worker: { id: 'w1', sequence: 1, speaker: 'worker', text: '你还好吗？', client_turn_id: 'voice-1' },
      client: { id: 'c2', sequence: 2, speaker: 'client', text: '我在听。', client_turn_id: 'voice-1' },
    }))
    expect(sentJson(socket).some((event) => event.type === 'playback.ended')).toBe(false)

    act(() => live.finishPlayback())
    expect(sentJson(socket).at(-1)).toEqual({ type: 'playback.ended' })
    expect(live.result.current.isPlaying).toBe(false)
  })

  it('VAD 在 listening 阶段更新显示并上报指标，不会自行触发手动交轮', () => {
    const voice = setup('voice')
    act(() => voice.sockets[0].open())
    expect(voice.result.current.voiceActivity).toEqual({
      state: 'quiet', confirmedSilenceMs: 0,
    })
    act(() => voice.getCaptureCallbacks().onVadCandidate({
      type: 'voice_start', atMs: 120, rms: 0.04, noiseFloor: 0.004,
    }))
    expect(voice.result.current.voiceActivity).toEqual({
      state: 'speaking', confirmedSilenceMs: 0,
    })
    act(() => voice.getCaptureCallbacks().onVadCandidate({
      type: 'voice_end', atMs: 620, rms: 0.004, noiseFloor: 0.004,
      confirmedSilenceMs: 450,
    }))
    expect(voice.result.current.voiceActivity).toEqual({
      state: 'paused', confirmedSilenceMs: 450,
    })
    const pcm = new Int16Array(320).buffer
    act(() => voice.getCaptureCallbacks().onPcmFrame(pcm))
    expect(sentJson(voice.sockets[0]).filter((event) => event.type.startsWith('vad.'))).toEqual([
      { type: 'vad.speech_started', at_ms: 120 },
      { type: 'vad.speech_stopped', at_ms: 620, confirmed_silence_ms: 450 },
    ])
    expect(sentJson(voice.sockets[0]).filter(
      (event) => event.type === 'turn.manual_complete',
    )).toHaveLength(0)
    expect(voice.sockets[0].sent).toContain(pcm)

    const text = setup('text')
    act(() => text.sockets[0].open())
    act(() => text.result.current.sendText('你好。'))
    expect(text.capture.start).not.toHaveBeenCalled()
    expect(sentJson(text.sockets[0]).at(-1)).toEqual(expect.objectContaining({
      type: 'text.turn', text: '你好。',
    }))
  })

  it('服务端进入生成或播放阶段后暂停 PCM 上行和 VAD 显示，回到 listening 才恢复', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    const initialPcm = new Int16Array([1]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(initialPcm))
    expect(socket.sent).toContain(initialPcm)

    for (const [index, phase] of ['directing', 'acting', 'synthesizing', 'playing'].entries()) {
      act(() => socket.json({ type: 'phase', phase }))
      const blockedPcm = new Int16Array([index + 2]).buffer
      act(() => {
        live.getCaptureCallbacks().onPcmFrame(blockedPcm)
        live.getCaptureCallbacks().onVadCandidate({
          type: 'voice_start', atMs: 200 + index, rms: 0.04, noiseFloor: 0.004,
        })
      })
      expect(socket.sent).not.toContain(blockedPcm)
      expect(live.result.current.voiceActivity.state).toBe('quiet')
    }

    act(() => socket.json({ type: 'phase', phase: 'listening' }))
    const resumedPcm = new Int16Array([9]).buffer
    act(() => {
      live.getCaptureCallbacks().onPcmFrame(resumedPcm)
      live.getCaptureCallbacks().onVadCandidate({
        type: 'voice_start', atMs: 900, rms: 0.04, noiseFloor: 0.004,
      })
    })
    expect(socket.sent).toContain(resumedPcm)
    expect(live.result.current.voiceActivity.state).toBe('speaking')
    expect(sentJson(socket).filter((event) => event.type.startsWith('vad.'))).toEqual([
      { type: 'vad.speech_started', at_ms: 900 },
    ])
    expect(live.capture.start).toHaveBeenCalledOnce()
    expect(live.capture.close).not.toHaveBeenCalled()
  })

  it('重连 snapshot 恢复在播放阶段时不上行麦克风内容', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({
      type: 'snapshot', media: 'voice', phase: 'playing', transcript: [],
    }))

    const blockedPcm = new Int16Array([3]).buffer
    act(() => {
      live.getCaptureCallbacks().onPcmFrame(blockedPcm)
      live.getCaptureCallbacks().onVadCandidate({
        type: 'voice_start', atMs: 300, rms: 0.04, noiseFloor: 0.004,
      })
    })

    expect(socket.sent).not.toContain(blockedPcm)
    expect(live.result.current.voiceActivity.state).toBe('quiet')
    expect(sentJson(socket).filter((event) => event.type.startsWith('vad.'))).toEqual([])
    expect(live.capture.start).toHaveBeenCalledOnce()
    expect(live.capture.close).not.toHaveBeenCalled()
  })

  it('手动提交成功后立即暂停 PCM 上行和 VAD 显示，下一次 listening 恢复', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => live.getCaptureCallbacks().onVadCandidate({
      type: 'voice_start', atMs: 120, rms: 0.04, noiseFloor: 0.004,
    }))
    expect(live.result.current.voiceActivity.state).toBe('speaking')

    let submitted = false
    act(() => { submitted = live.result.current.manualComplete() })
    expect(submitted).toBe(true)
    expect(live.result.current.voiceActivity.state).toBe('quiet')

    const blockedPcm = new Int16Array([4]).buffer
    act(() => {
      live.getCaptureCallbacks().onPcmFrame(blockedPcm)
      live.getCaptureCallbacks().onVadCandidate({
        type: 'voice_start', atMs: 400, rms: 0.04, noiseFloor: 0.004,
      })
    })
    expect(socket.sent).not.toContain(blockedPcm)
    expect(live.result.current.voiceActivity.state).toBe('quiet')
    expect(sentJson(socket).filter((event) => event.type.startsWith('vad.'))).toEqual([
      { type: 'vad.speech_started', at_ms: 120 },
    ])

    act(() => {
      socket.json({ type: 'phase', phase: 'acting' })
      socket.json({ type: 'input.error', message: '没有识别到有效内容' })
    })
    const processingPcm = new Int16Array([5]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(processingPcm))
    expect(socket.sent).not.toContain(processingPcm)

    act(() => socket.json({ type: 'phase', phase: 'listening' }))
    const resumedPcm = new Int16Array([6]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(resumedPcm))
    expect(socket.sent).toContain(resumedPcm)
    expect(live.capture.start).toHaveBeenCalledOnce()
    expect(live.capture.close).not.toHaveBeenCalled()
  })

  it('手动提交被 input.error 拒绝且仍在 listening 时恢复麦克风上行', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    let submitted = false
    act(() => { submitted = live.result.current.manualComplete() })
    expect(submitted).toBe(true)
    expect(live.result.current.canManualComplete).toBe(false)

    const pendingPcm = new Int16Array([7]).buffer
    act(() => live.getCaptureCallbacks().onPcmFrame(pendingPcm))
    expect(socket.sent).not.toContain(pendingPcm)

    act(() => socket.json({
      type: 'input.error', message: '没有识别到有效内容，请再说一次。',
    }))
    expect(live.result.current.canManualComplete).toBe(true)
    expect(live.result.current.inputError).toBe('没有识别到有效内容，请再说一次。')

    const resumedPcm = new Int16Array([8]).buffer
    act(() => {
      live.getCaptureCallbacks().onPcmFrame(resumedPcm)
      live.getCaptureCallbacks().onVadCandidate({
        type: 'voice_start', atMs: 800, rms: 0.04, noiseFloor: 0.004,
      })
    })
    expect(socket.sent).toContain(resumedPcm)
    expect(sentJson(socket).filter((event) => event.type.startsWith('vad.'))).toEqual([
      { type: 'vad.speech_started', at_ms: 800 },
    ])
    expect(live.capture.start).toHaveBeenCalledOnce()
    expect(live.capture.close).not.toHaveBeenCalled()
  })

  it('新一次手动提交和成功提交都会清除旧输入错误', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({
      type: 'input.error', message: '上一轮没有听清，请再说一次。',
    }))
    expect(live.result.current.inputError).toBe('上一轮没有听清，请再说一次。')

    act(() => live.result.current.manualComplete())
    expect(live.result.current.inputError).toBe('')

    act(() => socket.json({
      type: 'input.error', message: '这条旧提示也应在成功后消失。',
    }))
    act(() => socket.json({
      type: 'turn.committed', client_turn_id: 'voice-clear-error',
      worker: {
        id: 'w-clear', sequence: 1, speaker: 'worker',
        text: '我想继续了解一下。', client_turn_id: 'voice-clear-error',
      },
      client: {
        id: 'c-clear', sequence: 2, speaker: 'client',
        text: '嗯，你问吧。', client_turn_id: 'voice-clear-error',
      },
    }))
    expect(live.result.current.inputError).toBe('')
  })

  it('只在可发言阶段接受一次手动交轮，生成和播放期间拒绝提交', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    let first = false
    let duplicate = true
    act(() => {
      first = live.result.current.manualComplete()
      duplicate = live.result.current.manualComplete()
    })
    expect(first).toBe(true)
    expect(duplicate).toBe(false)
    expect(sentJson(socket).filter((event) => event.type === 'turn.manual_complete')).toHaveLength(1)

    act(() => socket.json({ type: 'phase', phase: 'acting' }))
    let whileGenerating = true
    act(() => { whileGenerating = live.result.current.manualComplete() })
    expect(whileGenerating).toBe(false)

    act(() => socket.json({ type: 'phase', phase: 'listening' }))
    act(() => socket.binary())
    let whilePlaying = true
    act(() => { whilePlaying = live.result.current.manualComplete() })
    expect(whilePlaying).toBe(false)

    act(() => live.finishPlayback())
    let nextTurn = false
    act(() => { nextTurn = live.result.current.manualComplete() })
    expect(nextTurn).toBe(true)
    expect(sentJson(socket).filter((event) => event.type === 'turn.manual_complete')).toHaveLength(2)
  })

  it('连接意外断开后自动重连，并以新 snapshot 恢复原文', async () => {
    vi.useFakeTimers()
    const live = setup('text')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].json({
      type: 'snapshot', media: 'text', phase: 'listening',
      transcript: [{ id: 'c1', sequence: 1, speaker: 'client', text: '第一句', client_turn_id: 'opening' }],
    }))

    act(() => live.sockets[0].serverClose())
    expect(live.result.current.connection).toBe('reconnecting')
    expect(live.result.current.phase).toBe('technical_paused')
    expect(live.result.current.technicalPause).toEqual({
      message: '来访者的信号不太稳定', canRetry: true,
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(live.sockets).toHaveLength(2)

    act(() => live.sockets[1].open())
    act(() => live.sockets[1].json({
      type: 'snapshot', media: 'text', phase: 'listening',
      transcript: [
        { id: 'c1', sequence: 1, speaker: 'client', text: '第一句', client_turn_id: 'opening' },
        { id: 'w2', sequence: 2, speaker: 'worker', text: '第二句', client_turn_id: 'turn-1' },
      ],
    }))
    expect(live.result.current.transcript.map((turn) => turn.text)).toEqual(['第一句', '第二句'])
    expect(live.result.current.technicalPause).toBeNull()
    expect(live.result.current.phase).toBe('listening')
  })

  it('连接意外断开时清掉重说提示，只保留信号不稳定状态', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({ type: 'input.reset', message: '已清空，请重新说这一句' }))
    expect(live.result.current.inputNotice).toBe('已清空，请重新说这一句')

    act(() => socket.serverClose())

    expect(live.result.current.inputNotice).toBe('')
    expect(live.result.current.technicalPause).toEqual({
      message: '来访者的信号不太稳定', canRetry: true,
    })
  })

  it('切换到新会话时同步清理上一会话的临时输入状态', () => {
    const live = setup('voice')
    const firstSocket = live.sockets[0]
    act(() => firstSocket.open())
    act(() => firstSocket.json({
      type: 'snapshot', media: 'voice', phase: 'listening',
      transcript: [{ id: 'old-turn', sequence: 1, speaker: 'worker', text: '上一会话原文', client_turn_id: 'old' }],
    }))
    act(() => firstSocket.json({ type: 'asr.partial', transcript: '上一会话未提交内容' }))

    live.rerender({ sessionId: 'session-2' })

    expect(live.result.current.liveTranscript).toBe('')
    expect(live.result.current.transcript).toEqual([])
    expect(live.result.current.inputNotice).toBe('')

    const secondSocket = live.sockets[1]
    act(() => secondSocket.open())
    act(() => secondSocket.json({ type: 'input.reset', message: '已清空，请重新说这一句' }))
    expect(live.result.current.inputNotice).toBe('已清空，请重新说这一句')

    live.rerender({ sessionId: 'session-3' })

    expect(live.result.current.inputNotice).toBe('')
  })

  it('已结束会话切换到新会话时恢复完整初始状态', () => {
    const live = setup('voice')
    const firstSocket = live.sockets[0]
    act(() => firstSocket.open())
    act(() => firstSocket.binary())
    act(() => firstSocket.json({ type: 'session.ended', reason: 'natural_closure' }))
    expect(live.result.current.endedReason).toBe('natural_closure')

    live.rerender({ sessionId: 'session-2' })

    expect(live.result.current.endedReason).toBeNull()
    expect(live.result.current.phase).toBe('listening')
    expect(live.result.current.connection).toBe('connecting')
    expect(live.result.current.technicalPause).toBeNull()
    expect(live.result.current.inputError).toBe('')
    expect(live.result.current.isPlaying).toBe(false)
    expect(live.result.current.energy).toBe(0)
  })

  it('切换会话时丢弃旧技术暂停与待补发的客户端失败', () => {
    const live = setup('voice')
    const firstSocket = live.sockets[0]
    act(() => firstSocket.open())
    firstSocket.readyState = 3
    act(() => live.getCaptureCallbacks().onError?.(
      new LiveAudioCaptureError('microphone_ended', '麦克风断开'),
    ))
    expect(live.result.current.technicalPause).not.toBeNull()

    live.rerender({ sessionId: 'session-2' })
    const secondSocket = live.sockets[1]
    act(() => secondSocket.open())

    expect(live.result.current.technicalPause).toBeNull()
    expect(live.result.current.phase).toBe('listening')
    expect(sentJson(secondSocket)).toEqual([{ type: 'session.start' }])
  })

  it('语音重连后丢弃未提交转写，并提示整句重说', async () => {
    vi.useFakeTimers()
    const live = setup('voice')
    const firstSocket = live.sockets[0]
    act(() => firstSocket.open())
    act(() => firstSocket.json({
      type: 'snapshot', media: 'voice', phase: 'listening', transcript: [], can_redo_input: true,
    }))
    act(() => firstSocket.json({ type: 'asr.partial', transcript: '旧的未提交内容' }))
    act(() => { live.result.current.redoInput() })
    expect(live.result.current.redoInputPending).toBe(true)

    act(() => firstSocket.serverClose())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    const secondSocket = live.sockets[1]
    act(() => secondSocket.open())
    act(() => secondSocket.json({
      type: 'snapshot', media: 'voice', phase: 'listening', transcript: [], can_redo_input: true,
    }))

    expect(live.result.current.liveTranscript).toBe('')
    expect(live.result.current.redoInputPending).toBe(false)
    expect(live.result.current.inputError).toBe('刚才这句话没有完整送达，请整句重新说一遍')
  })

  it('限制主线程能量状态的刷新频率', () => {
    let now = 0
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    const live = setup('voice')
    act(() => live.sockets[0].open())

    act(() => live.getCaptureCallbacks().onEnergy(0.01))
    expect(live.result.current.energy).toBe(0.01)
    now = 10
    act(() => live.getCaptureCallbacks().onEnergy(0.05))
    expect(live.result.current.energy).toBe(0.01)
    now = 60
    act(() => live.getCaptureCallbacks().onEnergy(0.03))
    expect(live.result.current.energy).toBe(0.03)
  })

  it('麦克风中断时先释放旧采集链路，重试时重新启动', async () => {
    const live = setup('voice')
    act(() => live.sockets[0].open())
    act(() => live.getCaptureCallbacks().onError?.(
      new LiveAudioCaptureError('microphone_ended', '麦克风连接已经中断'),
    ))

    await waitFor(() => expect(live.capture.close).toHaveBeenCalledOnce())
    expect(sentJson(live.sockets[0])).toContainEqual({
      type: 'client.failure', stage: 'capture', code: 'microphone_ended',
    })
    expect(live.result.current.technicalPause).toEqual({
      message: '这边的声音刚刚断开了，请检查麦克风连接后重试。',
      canRetry: true,
    })
    act(() => live.result.current.retry())
    expect(live.capture.start).toHaveBeenCalledOnce()

    act(() => live.sockets[0].json({ type: 'phase', phase: 'listening' }))
    await waitFor(() => expect(live.capture.start).toHaveBeenCalledTimes(2))
  })

  it('连接断开期间暂存采集故障，并在重连后补发原始错误码', async () => {
    vi.useFakeTimers()
    const live = setup('voice')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].serverClose())
    act(() => live.getCaptureCallbacks().onError?.(
      new LiveAudioCaptureError('permission_denied', '没有获得麦克风权限'),
    ))

    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    act(() => live.sockets[1].open())

    expect(sentJson(live.sockets[1])).toEqual([
      { type: 'session.start' },
      { type: 'client.failure', stage: 'capture', code: 'permission_denied' },
    ])
    expect(live.result.current.technicalPause?.message).toBe(
      '这边暂时听不到你的声音，请允许浏览器使用麦克风后重试。',
    )
  })

  it('播放失败单独上报且不会关闭麦克风采集链路', async () => {
    const live = setup('voice')
    live.playback.queue.mockRejectedValueOnce(new Error('audio output failed'))
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].binary())

    await waitFor(() => expect(sentJson(live.sockets[0])).toContainEqual({
      type: 'client.failure', stage: 'playback', code: 'playback_failed',
    }))
    expect(live.capture.close).not.toHaveBeenCalled()
    expect(live.result.current.technicalPause?.message).toBe(
      '来访者的声音没有正常播放，本次会谈需要先停下来确认。',
    )
  })

  it('会话不存在的终态关闭码 4404 不再触发重连', async () => {
    vi.useFakeTimers()
    const live = setup('text')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].serverClose(4404))

    await act(async () => { await vi.advanceTimersByTimeAsync(30) })

    expect(live.sockets).toHaveLength(1)
    expect(live.result.current.connection).toBe('closed')
  })

  it('收到 session.error 后停止后续重连', async () => {
    vi.useFakeTimers()
    const live = setup('voice')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].json({ type: 'session.error', message: '会话已结束' }))
    act(() => live.sockets[0].serverClose())

    await act(async () => { await vi.advanceTimersByTimeAsync(30) })

    expect(live.sockets).toHaveLength(1)
    expect(live.result.current.connection).toBe('closed')
    expect(live.result.current.inputError).toBe('会话已结束')
    expect(live.capture.close).toHaveBeenCalled()
    expect(live.playback.stop).toHaveBeenCalled()
  })

  it('只在 listening 阶段发送文字回合', () => {
    const live = setup('text')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.json({ type: 'phase', phase: 'directing' }))

    let sent = true
    act(() => { sent = live.result.current.sendText('请继续说。') })

    expect(sent).toBe(false)
    expect(sentJson(socket).filter((event) => event.type === 'text.turn')).toHaveLength(0)
  })

  it('在线咨询不创建麦克风或语音播放链路，并忽略二进制音频', () => {
    const live = setup('text')
    const socket = live.sockets[0]
    act(() => socket.open())

    expect(live.capture.start).not.toHaveBeenCalled()
    expect(live.createPlayback).not.toHaveBeenCalled()
    act(() => socket.binary())
    expect(live.playback.queue).not.toHaveBeenCalled()
    expect(live.result.current.isPlaying).toBe(false)
  })

  it('在线文字发送期间拒绝重复提交，失败后保留状态并允许重试', () => {
    const live = setup('text')
    const socket = live.sockets[0]
    act(() => socket.open())

    let first = false
    let duplicate = true
    act(() => {
      first = live.result.current.sendText('我先听你说。')
      duplicate = live.result.current.sendText('我先听你说。')
    })
    expect(first).toBe(true)
    expect(duplicate).toBe(false)
    expect(live.result.current.textTurnStatus).toBe('pending')
    expect(sentJson(socket).filter((event) => event.type === 'text.turn')).toHaveLength(1)

    act(() => socket.json({ type: 'input.error', message: '这条消息没有送达，请再试一次。' }))
    expect(live.result.current.textTurnStatus).toBe('failed')
    expect(live.result.current.inputError).toBe('这条消息没有送达，请再试一次。')

    let retried = false
    act(() => { retried = live.result.current.sendText('我先听你说。') })
    expect(retried).toBe(true)
    expect(live.result.current.textTurnStatus).toBe('pending')
    expect(sentJson(socket).filter((event) => event.type === 'text.turn')).toHaveLength(2)
  })

  it('在线来访者的一次回复按换行依次显现，提交后仍只保存一个话轮', async () => {
    vi.useFakeTimers()
    const live = setup('text', { textMessageIntervalMs: 40 } as LiveSessionDependencies)
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => socket.json({
      type: 'visitor.text',
      text: '第一句\n\n第二句\r\n第三句',
    }))
    expect(live.result.current.visitorReveal).toEqual({
      turnId: null,
      visibleSegments: [],
      isTyping: true,
    })

    await act(async () => { await vi.advanceTimersByTimeAsync(40) })
    expect(live.result.current.visitorReveal).toEqual({
      turnId: null,
      visibleSegments: ['第一句'],
      isTyping: true,
    })

    act(() => socket.json({
      type: 'turn.committed', client_turn_id: 'online-1',
      worker: {
        id: 'w-online', sequence: 1, speaker: 'worker',
        text: '你愿意接着说吗？', client_turn_id: 'online-1',
      },
      client: {
        id: 'c-online', sequence: 2, speaker: 'client',
        text: '第一句\n\n第二句\r\n第三句', client_turn_id: 'online-1',
      },
    }))
    expect(live.result.current.transcript.filter((turn) => turn.id === 'c-online')).toHaveLength(1)
    expect(live.result.current.visitorReveal?.turnId).toBe('c-online')

    await act(async () => { await vi.advanceTimersByTimeAsync(80) })
    expect(live.result.current.visitorReveal).toEqual({
      turnId: 'c-online',
      visibleSegments: ['第一句', '第二句', '第三句'],
      isTyping: false,
    })
  })

  it('WebSocket 已断开时通过 REST 完成技术中断并收口页面', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(endedSessionResponse('technical_interruption')))
    const live = setup('voice')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].json({
      type: 'technical.pause', phase: 'technical_paused',
      message: '来访者的信号不太稳定', can_retry: true,
    }))
    live.sockets[0].readyState = 3

    act(() => live.result.current.endSession())
    await waitFor(() => expect(live.result.current.endedReason).toBe('technical_interruption'))
    expect(live.capture.close).toHaveBeenCalled()
    expect(live.playback.stop).toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledWith('/api/sessions/session-1/end', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ reason: 'technical_interruption' }),
    }))
  })

  it('session.end 已写入但确认前断线时只调用一次 REST 并进入后端终态', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(endedSessionResponse('user_ended'))
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => live.result.current.endSession())
    expect(sentJson(socket).filter((event) => event.type === 'session.end')).toHaveLength(1)
    expect(fetchMock).not.toHaveBeenCalled()

    act(() => socket.serverClose())
    await act(async () => { await Promise.resolve() })

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/end', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ reason: 'user_ended' }),
    }))
    expect(live.result.current.endedReason).toBe('user_ended')
    expect(live.result.current.phase).toBe('ended')
    expect(live.result.current.connection).toBe('closed')

    await act(async () => { await vi.advanceTimersByTimeAsync(30) })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('session.end 一直没有确认时在短超时后用 REST 收口', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(endedSessionResponse('user_ended'))
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => live.result.current.endSession())
    await act(async () => { await vi.advanceTimersByTimeAsync(19) })
    expect(fetchMock).not.toHaveBeenCalled()

    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(live.result.current.endedReason).toBe('user_ended')
    expect(live.result.current.phase).toBe('ended')
  })

  it('及时收到 session.ended 时取消超时兜底且不调用 REST', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())

    act(() => {
      live.result.current.endSession()
      live.result.current.endSession()
    })

    expect(sentJson(socket).filter((event) => event.type === 'session.end')).toHaveLength(1)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(live.result.current.endedReason).toBeNull()
    expect(live.capture.close).toHaveBeenCalled()
    expect(live.playback.stop).toHaveBeenCalled()

    act(() => socket.json({ type: 'session.ended', reason: 'user_ended' }))
    expect(live.result.current.endedReason).toBe('user_ended')

    await act(async () => { await vi.advanceTimersByTimeAsync(30) })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('等待结束确认时卸载会取消计时器且不重复调用 REST', async () => {
    vi.useFakeTimers()
    let resolveFetch!: (response: Response) => void
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => live.result.current.endSession())

    live.unmount()
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(socket.onopen).toBeNull()
    expect(socket.onmessage).toBeNull()
    expect(socket.onclose).toBeNull()
    expect(socket.onerror).toBeNull()
    expect(socket.closeCalls).toContainEqual({
      code: 1000,
      reason: 'component_disposed',
    })

    act(() => socket.json({ type: 'session.ended', reason: 'user_ended' }))
    expect(live.result.current.endedReason).toBeNull()

    await act(async () => { await vi.advanceTimersByTimeAsync(30) })
    expect(fetchMock).toHaveBeenCalledOnce()

    await act(async () => {
      resolveFetch(endedSessionResponse('user_ended'))
      await Promise.resolve()
    })
    expect(live.result.current.endedReason).toBeNull()
  })

  it('切换会话后旧会话的延迟 REST 响应不会结束新会话', async () => {
    const resolveFetch: Array<(response: Response) => void> = []
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      resolveFetch.push(resolve)
    }))
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('text')
    act(() => live.sockets[0].open())
    act(() => live.result.current.endSession())

    live.rerender({ sessionId: 'session-2' })
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/end', expect.anything())
    expect(live.sockets).toHaveLength(2)
    act(() => live.sockets[1].open())
    live.sockets[1].readyState = 3
    act(() => live.result.current.endSession())
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-2/end', expect.anything())

    await act(async () => {
      resolveFetch[0](endedSessionResponse('user_ended'))
      await Promise.resolve()
    })

    expect(live.result.current.endedReason).toBeNull()
    expect(live.result.current.phase).toBe('listening')

    await act(async () => {
      resolveFetch[1](endedSessionResponse('user_ended', 'session-2'))
      await Promise.resolve()
    })
    expect(live.result.current.endedReason).toBe('user_ended')
    expect(live.result.current.phase).toBe('ended')
  })

  it('切换会话后旧 WebSocket 的迟到关闭不会改写或重连新会话', async () => {
    vi.useFakeTimers()
    const live = setup('text')
    const oldSocket = live.sockets[0]
    act(() => oldSocket.open())

    live.rerender({ sessionId: 'session-2' })
    expect(live.sockets).toHaveLength(2)
    act(() => live.sockets[1].open())
    act(() => oldSocket.serverClose())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })

    expect(live.sockets).toHaveLength(2)
    expect(live.result.current.connection).toBe('connected')
    expect(live.result.current.phase).toBe('listening')
  })

  it('重连收到 4409 时通过幂等 REST 读取既有自然结束原因', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(endedSessionResponse('natural_closure'))
    vi.stubGlobal('fetch', fetchMock)
    const live = setup('voice')
    act(() => live.sockets[0].open())
    act(() => live.sockets[0].serverClose())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(live.sockets).toHaveLength(2)

    act(() => live.sockets[1].open())
    act(() => live.sockets[1].json({ type: 'session.error', message: '会话已结束' }))
    act(() => live.sockets[1].serverClose(4409))
    await act(async () => { await Promise.resolve() })

    expect(live.result.current.endedReason).toBe('natural_closure')
    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/end', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ reason: 'user_ended' }),
    }))
    expect(live.result.current.phase).toBe('ended')
    expect(live.result.current.connection).toBe('closed')
    expect(live.result.current.inputError).toBe('')
  })

  it('自然结束事件立即停止收音和播放并保留结束原因', () => {
    const live = setup('voice')
    const socket = live.sockets[0]
    act(() => socket.open())
    act(() => socket.binary())

    act(() => socket.json({ type: 'session.ended', reason: 'natural_closure' }))

    expect(live.result.current.endedReason).toBe('natural_closure')
    expect(live.result.current.phase).toBe('ended')
    expect(live.capture.close).toHaveBeenCalled()
    expect(live.playback.stop).toHaveBeenCalled()
    expect(live.result.current.isPlaying).toBe(false)
  })
})
