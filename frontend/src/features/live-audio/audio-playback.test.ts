import { describe, expect, it, vi } from 'vitest'

import { PcmAudioPlayback } from './audio-playback'

class FakeAudioBuffer {
  readonly length: number
  readonly sampleRate: number
  readonly channel: Float32Array

  constructor(length: number, sampleRate: number) {
    this.length = length
    this.sampleRate = sampleRate
    this.channel = new Float32Array(length)
  }

  get duration() {
    return this.length / this.sampleRate
  }

  copyToChannel(source: Float32Array) {
    this.channel.set(source)
  }
}

class FakeBufferSource {
  buffer: FakeAudioBuffer | null = null
  onended: (() => void) | null = null
  connect = vi.fn()
  start = vi.fn()
  stop = vi.fn()
  disconnect = vi.fn()
}

class FakePlaybackContext {
  state: AudioContextState = 'suspended'
  currentTime = 10
  destination = {}
  buffers: FakeAudioBuffer[] = []
  sources: FakeBufferSource[] = []
  resume = vi.fn().mockImplementation(async () => {
    this.state = 'running'
  })
  close = vi.fn().mockImplementation(async () => {
    this.state = 'closed'
  })
  createBuffer = vi.fn((_channels: number, length: number, sampleRate: number) => {
    const buffer = new FakeAudioBuffer(length, sampleRate)
    this.buffers.push(buffer)
    return buffer
  })
  createBufferSource = vi.fn(() => {
    const source = new FakeBufferSource()
    this.sources.push(source)
    return source
  })
}

describe('PcmAudioPlayback', () => {
  it('按 24 kHz PCM16 分片的收到顺序无缝排队', async () => {
    const context = new FakePlaybackContext()
    const playback = new PcmAudioPlayback({
      createAudioContext: () => context as unknown as AudioContext,
      startLeadSeconds: 0.04,
    })
    const first = new Int16Array(480).fill(16_384)
    const second = new Int16Array(240).fill(-32_768)

    await playback.queue(first.buffer)
    await playback.queue(second.buffer)

    expect(context.resume).toHaveBeenCalledOnce()
    expect(context.buffers.map((buffer) => [buffer.length, buffer.sampleRate])).toEqual([
      [480, 24_000],
      [240, 24_000],
    ])
    expect(context.buffers[0].channel[0]).toBeCloseTo(0.5, 4)
    expect(context.buffers[1].channel[0]).toBe(-1)
    expect(context.sources[0].start).toHaveBeenCalledWith(10.04)
    expect(context.sources[1].start.mock.calls[0][0]).toBeCloseTo(10.06, 6)
    expect(playback.isPlaying).toBe(true)
  })

  it('stop 清空排队音频，close 同时关闭 AudioContext', async () => {
    const context = new FakePlaybackContext()
    const playback = new PcmAudioPlayback({
      createAudioContext: () => context as unknown as AudioContext,
    })
    await playback.queue(new Int16Array(480).buffer)

    playback.stop()

    expect(context.sources[0].stop).toHaveBeenCalledOnce()
    expect(playback.isPlaying).toBe(false)

    await playback.close()
    expect(context.close).toHaveBeenCalledOnce()
  })

  it('首次 resume 尚未完成时并发分片仍按调用顺序串行处理', async () => {
    let finishResume: (() => void) | undefined
    const context = new FakePlaybackContext()
    context.resume = vi.fn(() => new Promise<void>((resolve) => {
      finishResume = () => {
        context.state = 'running'
        resolve()
      }
    }))
    const playback = new PcmAudioPlayback({
      createAudioContext: () => context as unknown as AudioContext,
    })

    const first = playback.queue(new Int16Array(480).fill(10_000).buffer)
    const second = playback.queue(new Int16Array(480).fill(-10_000).buffer)
    await Promise.resolve()

    expect(context.resume).toHaveBeenCalledOnce()
    finishResume?.()
    await Promise.all([first, second])
    expect(context.buffers[0].channel[0]).toBeGreaterThan(0)
    expect(context.buffers[1].channel[0]).toBeLessThan(0)
  })

  it('按小端字节解释服务端 PCM16', async () => {
    const context = new FakePlaybackContext()
    const playback = new PcmAudioPlayback({
      createAudioContext: () => context as unknown as AudioContext,
    })
    const pcm = Uint8Array.from([0x00, 0x40, 0x00, 0x80]).buffer

    await playback.queue(pcm)

    expect(context.buffers[0].channel[0]).toBeCloseTo(0.5, 4)
    expect(context.buffers[0].channel[1]).toBe(-1)
  })

  it('全部分片自然播放结束后只通知一次，主动停止不通知', async () => {
    const context = new FakePlaybackContext()
    const onIdle = vi.fn()
    const playback = new PcmAudioPlayback({
      createAudioContext: () => context as unknown as AudioContext,
      onIdle,
    })

    await playback.queue(new Int16Array(480).buffer)
    await playback.queue(new Int16Array(480).buffer)
    context.sources[0].onended?.()
    expect(onIdle).not.toHaveBeenCalled()
    context.sources[1].onended?.()
    expect(onIdle).toHaveBeenCalledOnce()

    await playback.queue(new Int16Array(480).buffer)
    playback.stop()
    context.sources[2].onended?.()
    expect(onIdle).toHaveBeenCalledOnce()
  })
})
