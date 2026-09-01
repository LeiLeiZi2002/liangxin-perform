export class LiveAudioPlaybackError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'LiveAudioPlaybackError'
  }
}

type PlaybackOptions = {
  createAudioContext?: () => AudioContext
  startLeadSeconds?: number
  onIdle?: () => void
}

const PLAYBACK_SAMPLE_RATE = 24_000

export class PcmAudioPlayback {
  private readonly createAudioContext: () => AudioContext
  private readonly startLeadSeconds: number
  private readonly onIdle?: () => void
  private context: AudioContext | null = null
  private nextStartTime = 0
  private readonly sources = new Set<AudioBufferSourceNode>()
  private playing = false
  private queueTail = Promise.resolve()

  constructor(options: PlaybackOptions = {}) {
    this.createAudioContext = options.createAudioContext ??
      (() => new AudioContext({ latencyHint: 'interactive' }))
    this.startLeadSeconds = options.startLeadSeconds ?? 0.04
    this.onIdle = options.onIdle
  }

  get isPlaying() {
    return this.playing
  }

  queue(pcm: ArrayBuffer) {
    const operation = this.queueTail.then(() => this.schedule(pcm))
    this.queueTail = operation.catch(() => undefined)
    return operation
  }

  private async schedule(pcm: ArrayBuffer) {
    if (pcm.byteLength === 0) return
    if (pcm.byteLength % 2 !== 0) {
      throw new LiveAudioPlaybackError('收到的 PCM 音频长度不完整')
    }
    const context = this.context ?? this.createContext()
    if (context.state === 'closed') {
      throw new LiveAudioPlaybackError('音频播放环境已经关闭')
    }
    if (context.state === 'suspended') await context.resume()

    const sampleCount = pcm.byteLength / 2
    const bytes = new DataView(pcm)
    const floats = new Float32Array(sampleCount)
    for (let index = 0; index < sampleCount; index += 1) {
      const sample = bytes.getInt16(index * 2, true)
      floats[index] = sample < 0 ? sample / 32_768 : sample / 32_767
    }
    const buffer = context.createBuffer(1, floats.length, PLAYBACK_SAMPLE_RATE)
    buffer.copyToChannel(floats, 0)
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    const startAt = Math.max(this.nextStartTime, context.currentTime + this.startLeadSeconds)
    source.start(startAt)
    this.nextStartTime = startAt + buffer.duration
    this.sources.add(source)
    this.playing = true
    source.onended = () => {
      source.disconnect()
      const endedNaturally = this.sources.delete(source)
      if (endedNaturally && this.sources.size === 0) {
        this.playing = false
        this.nextStartTime = 0
        this.onIdle?.()
      }
    }
  }

  stop() {
    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        // 已经自然结束的 AudioBufferSourceNode 无需再次停止。
      }
      source.disconnect()
    }
    this.sources.clear()
    this.nextStartTime = 0
    this.playing = false
  }

  async close() {
    this.stop()
    if (this.context && this.context.state !== 'closed') await this.context.close()
    this.context = null
  }

  private createContext() {
    this.context = this.createAudioContext()
    return this.context
  }
}
