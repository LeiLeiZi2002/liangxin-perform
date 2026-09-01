import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  AdaptiveVad,
  LiveAudioCapture,
  LiveAudioCaptureError,
  type VadCandidate,
} from './audio-capture'

class FakeAudioNode {
  disconnect = vi.fn()
  connect = vi.fn(() => this)
}

class FakeAudioContext {
  state: AudioContextState = 'running'
  destination = new FakeAudioNode()
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) }
  source = new FakeAudioNode()
  gain = Object.assign(new FakeAudioNode(), { gain: { value: 1 } })
  resume = vi.fn().mockResolvedValue(undefined)
  close = vi.fn().mockImplementation(async () => {
    this.state = 'closed'
  })
  createMediaStreamSource = vi.fn(() => this.source)
  createGain = vi.fn(() => this.gain)
}

class FakeWorkletNode extends FakeAudioNode {
  static instances: FakeWorkletNode[] = []
  port: { onmessage: ((event: MessageEvent) => void) | null } = { onmessage: null }

  constructor() {
    super()
    FakeWorkletNode.instances.push(this)
  }
}

describe('AdaptiveVad', () => {
  it('根据安静环境基线只发出开始和结束候选，不提交最终话轮', () => {
    const vad = new AdaptiveVad({ startHoldMs: 80, endHoldMs: 400, calibrationMs: 200 })
    const events: VadCandidate[] = []

    for (let time = 0; time <= 200; time += 40) {
      const candidate = vad.observe(0.004, time)
      if (candidate) events.push(candidate)
    }
    vad.observe(0.05, 240)
    const start = vad.observe(0.05, 330)
    vad.observe(0.004, 400)
    const end = vad.observe(0.004, 820)

    if (start) events.push(start)
    if (end) events.push(end)
    expect(events.map((event) => event.type)).toEqual(['voice_start', 'voice_end'])
    expect(events.every((event) => 'complete' in event === false)).toBe(true)
    expect(end).toEqual(expect.objectContaining({ confirmedSilenceMs: 420 }))
  })

  it('启动时持续的中等环境噪声会进入基线而不是误报说话', () => {
    const vad = new AdaptiveVad({ calibrationMs: 500 })
    const events = Array.from({ length: 21 }, (_, index) => vad.observe(0.02, index * 50))

    expect(events.filter(Boolean)).toEqual([])
  })

  it('跟随缓慢变化的环境噪声，真语音叠加后仍能发出开始候选', () => {
    const vad = new AdaptiveVad({ calibrationMs: 300, startHoldMs: 80 })
    const events: VadCandidate[] = []
    for (let time = 0; time <= 1000; time += 50) {
      const rms = 0.006 + time / 1000 * 0.008
      const event = vad.observe(rms, time)
      if (event) events.push(event)
    }
    vad.observe(0.06, 1100)
    const voice = vad.observe(0.06, 1200)
    if (voice) events.push(voice)

    expect(events.map((event) => event.type)).toEqual(['voice_start'])
  })

  it('reset 会丢弃上一段采集的说话与校准状态', () => {
    const vad = new AdaptiveVad({ calibrationMs: 200, startHoldMs: 80 })
    for (let time = 0; time <= 200; time += 50) vad.observe(0.004, time)
    vad.observe(0.05, 250)
    expect(vad.observe(0.05, 340)?.type).toBe('voice_start')

    vad.reset()

    for (let time = 0; time <= 200; time += 50) {
      expect(vad.observe(0.004, time)).toBeNull()
    }
    vad.observe(0.05, 250)
    expect(vad.observe(0.05, 340)?.type).toBe('voice_start')
  })
})

describe('LiveAudioCapture', () => {
  beforeEach(() => {
    FakeWorkletNode.instances = []
  })

  it('持续开启带回声处理的麦克风并转发 PCM、能量和 VAD 候选', async () => {
    const trackStop = vi.fn()
    const track = { stop: trackStop, addEventListener: vi.fn() }
    const stream = { getTracks: () => [track] } as unknown as MediaStream
    const getUserMedia = vi.fn().mockResolvedValue(stream)
    const context = new FakeAudioContext()
    const onPcmFrame = vi.fn()
    const onEnergy = vi.fn()
    const onVadCandidate = vi.fn()
    const capture = new LiveAudioCapture(
      { onPcmFrame, onEnergy, onVadCandidate },
      {
        mediaDevices: { getUserMedia } as unknown as MediaDevices,
        createAudioContext: () => context as unknown as AudioContext,
        createWorkletNode: () => new FakeWorkletNode() as unknown as AudioWorkletNode,
        workletModuleUrl: '/assets/capture-processor.js',
      },
    )

    await capture.start()

    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    })
    expect(context.audioWorklet.addModule).toHaveBeenCalledWith('/assets/capture-processor.js')
    expect(context.gain.gain.value).toBe(0)
    expect(capture.isActive).toBe(true)

    const node = FakeWorkletNode.instances[0]
    const pcm = new Int16Array(320).buffer
    node.port.onmessage?.({ data: { type: 'pcm', pcm } } as MessageEvent)
    for (let atMs = 0; atMs <= 500; atMs += 100) {
      node.port.onmessage?.({ data: { type: 'energy', rms: 0.004, atMs } } as MessageEvent)
    }
    node.port.onmessage?.({ data: { type: 'energy', rms: 0.03, atMs: 600 } } as MessageEvent)
    node.port.onmessage?.({ data: { type: 'energy', rms: 0.03, atMs: 700 } } as MessageEvent)

    expect(onPcmFrame).toHaveBeenCalledWith(pcm)
    expect(onEnergy).toHaveBeenCalledWith(0.03)
    expect(onVadCandidate).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'voice_start' }),
    )

    await capture.close()
    expect(trackStop).toHaveBeenCalledOnce()
    expect(context.close).toHaveBeenCalledOnce()
    expect(capture.isActive).toBe(false)
  })

  it('将麦克风授权拒绝转换为明确错误', async () => {
    const getUserMedia = vi
      .fn()
      .mockRejectedValue(new DOMException('permission denied', 'NotAllowedError'))
    const capture = new LiveAudioCapture(
      { onPcmFrame: vi.fn(), onEnergy: vi.fn(), onVadCandidate: vi.fn() },
      { mediaDevices: { getUserMedia } as unknown as MediaDevices },
    )

    await expect(capture.start()).rejects.toEqual(
      expect.objectContaining<Partial<LiveAudioCaptureError>>({
        code: 'permission_denied',
        message: '没有获得麦克风权限',
      }),
    )
  })

  it('授权尚未返回时 close 会让并发 start 共用一次请求并回收迟到的 stream', async () => {
    let resolveStream: ((stream: MediaStream) => void) | undefined
    const permission = new Promise<MediaStream>((resolve) => {
      resolveStream = resolve
    })
    const trackStop = vi.fn()
    const track = { stop: trackStop, addEventListener: vi.fn() }
    const stream = { getTracks: () => [track] } as unknown as MediaStream
    const getUserMedia = vi.fn(() => permission)
    const createAudioContext = vi.fn()
    const capture = new LiveAudioCapture(
      { onPcmFrame: vi.fn(), onEnergy: vi.fn(), onVadCandidate: vi.fn() },
      {
        mediaDevices: { getUserMedia } as unknown as MediaDevices,
        createAudioContext,
      },
    )

    const firstStart = capture.start()
    const secondStart = capture.start()
    const closing = capture.close()
    resolveStream?.(stream)
    await Promise.all([firstStart, secondStart, closing])

    expect(getUserMedia).toHaveBeenCalledOnce()
    expect(trackStop).toHaveBeenCalledOnce()
    expect(createAudioContext).not.toHaveBeenCalled()
    expect(capture.isActive).toBe(false)
  })

  it('运行中的麦克风 track 意外结束会通知调用方', async () => {
    let ended: (() => void) | undefined
    const track = {
      stop: vi.fn(),
      addEventListener: vi.fn((_type: string, listener: () => void) => {
        ended = listener
      }),
    }
    const context = new FakeAudioContext()
    const onError = vi.fn()
    const capture = new LiveAudioCapture(
      { onPcmFrame: vi.fn(), onEnergy: vi.fn(), onVadCandidate: vi.fn(), onError },
      {
        mediaDevices: {
          getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }),
        } as unknown as MediaDevices,
        createAudioContext: () => context as unknown as AudioContext,
        createWorkletNode: () => new FakeWorkletNode() as unknown as AudioWorkletNode,
      },
    )
    await capture.start()

    ended?.()

    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'microphone_ended' }),
    )
  })

  it('每次关闭后重新开始都会重新校准 VAD', async () => {
    const track = { stop: vi.fn(), addEventListener: vi.fn() }
    const stream = { getTracks: () => [track] } as unknown as MediaStream
    const contexts: FakeAudioContext[] = []
    const onVadCandidate = vi.fn()
    const capture = new LiveAudioCapture(
      { onPcmFrame: vi.fn(), onEnergy: vi.fn(), onVadCandidate },
      {
        mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream) } as unknown as MediaDevices,
        createAudioContext: () => {
          const context = new FakeAudioContext()
          contexts.push(context)
          return context as unknown as AudioContext
        },
        createWorkletNode: () => new FakeWorkletNode() as unknown as AudioWorkletNode,
      },
    )

    const sendFreshVoice = (node: FakeWorkletNode) => {
      for (let atMs = 0; atMs <= 500; atMs += 100) {
        node.port.onmessage?.({ data: { type: 'energy', rms: 0.004, atMs } } as MessageEvent)
      }
      node.port.onmessage?.({ data: { type: 'energy', rms: 0.03, atMs: 600 } } as MessageEvent)
      node.port.onmessage?.({ data: { type: 'energy', rms: 0.03, atMs: 700 } } as MessageEvent)
    }

    await capture.start()
    sendFreshVoice(FakeWorkletNode.instances[0])
    await capture.close()
    await capture.start()
    sendFreshVoice(FakeWorkletNode.instances[1])

    expect(contexts).toHaveLength(2)
    expect(onVadCandidate.mock.calls.map(([event]) => event.type)).toEqual([
      'voice_start', 'voice_start',
    ])
  })
})
