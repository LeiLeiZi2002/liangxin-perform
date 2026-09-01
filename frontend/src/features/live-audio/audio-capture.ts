import workletModuleUrl from './capture-processor.ts?worker&url'

type VadCandidateBase = {
  atMs: number
  rms: number
  noiseFloor: number
}

export type VadCandidate =
  | (VadCandidateBase & { type: 'voice_start' })
  | (VadCandidateBase & { type: 'voice_end'; confirmedSilenceMs: number })

type VadOptions = {
  startHoldMs?: number
  endHoldMs?: number
  minimumVoiceRms?: number
  startRatio?: number
  baselineSmoothing?: number
  calibrationMs?: number
}

export class AdaptiveVad {
  private readonly startHoldMs: number
  private readonly endHoldMs: number
  private readonly minimumVoiceRms: number
  private readonly startRatio: number
  private readonly baselineSmoothing: number
  private readonly calibrationMs: number
  private noiseFloor = 0.004
  private firstObservationMs: number | null = null
  private speaking = false
  private aboveSince: number | null = null
  private belowSince: number | null = null

  constructor(options: VadOptions = {}) {
    this.startHoldMs = options.startHoldMs ?? 80
    this.endHoldMs = options.endHoldMs ?? 450
    this.minimumVoiceRms = options.minimumVoiceRms ?? 0.012
    this.startRatio = options.startRatio ?? 3
    this.baselineSmoothing = options.baselineSmoothing ?? 0.04
    this.calibrationMs = options.calibrationMs ?? 500
  }

  reset() {
    this.noiseFloor = 0.004
    this.firstObservationMs = null
    this.speaking = false
    this.aboveSince = null
    this.belowSince = null
  }

  observe(rms: number, atMs: number): VadCandidate | null {
    this.firstObservationMs ??= atMs
    if (atMs - this.firstObservationMs <= this.calibrationMs) {
      const calibrationSmoothing = Math.max(0.15, this.baselineSmoothing)
      this.noiseFloor =
        this.noiseFloor * (1 - calibrationSmoothing) + rms * calibrationSmoothing
      this.aboveSince = null
      return null
    }
    const startThreshold = Math.max(this.minimumVoiceRms, this.noiseFloor * this.startRatio)
    const endThreshold = Math.max(this.minimumVoiceRms * 0.6, this.noiseFloor * 1.8)

    if (!this.speaking) {
      if (rms < startThreshold) this.updateNoiseFloor(rms)
      if (rms >= startThreshold) {
        this.aboveSince ??= atMs
        if (atMs - this.aboveSince >= this.startHoldMs) {
          this.speaking = true
          this.aboveSince = null
          return { type: 'voice_start', atMs, rms, noiseFloor: this.noiseFloor }
        }
      } else {
        this.aboveSince = null
      }
      return null
    }

    if (rms <= endThreshold) {
      this.belowSince ??= atMs
      if (atMs - this.belowSince >= this.endHoldMs) {
        const confirmedSilenceMs = Math.max(0, Math.round(atMs - this.belowSince))
        this.speaking = false
        this.belowSince = null
        this.updateNoiseFloor(rms)
        return {
          type: 'voice_end',
          atMs,
          rms,
          noiseFloor: this.noiseFloor,
          confirmedSilenceMs,
        }
      }
    } else {
      this.belowSince = null
    }
    return null
  }

  private updateNoiseFloor(rms: number) {
    this.noiseFloor =
      this.noiseFloor * (1 - this.baselineSmoothing) + rms * this.baselineSmoothing
  }
}

export type LiveAudioCaptureCallbacks = {
  onPcmFrame: (pcm: ArrayBuffer) => void
  onEnergy: (rms: number) => void
  onVadCandidate: (candidate: VadCandidate) => void
  onError?: (error: LiveAudioCaptureError) => void
}

type CaptureDependencies = {
  mediaDevices?: MediaDevices
  createAudioContext?: () => AudioContext
  createWorkletNode?: (context: AudioContext) => AudioWorkletNode
  workletModuleUrl?: string
}

export type LiveAudioCaptureErrorCode =
  | 'permission_denied'
  | 'capture_unavailable'
  | 'audio_context_closed'
  | 'microphone_ended'

export class LiveAudioCaptureError extends Error {
  readonly code: LiveAudioCaptureErrorCode

  constructor(code: LiveAudioCaptureErrorCode, message: string) {
    super(message)
    this.name = 'LiveAudioCaptureError'
    this.code = code
  }
}

export class LiveAudioCapture {
  private readonly callbacks: LiveAudioCaptureCallbacks
  private readonly dependencies: CaptureDependencies
  private readonly vad = new AdaptiveVad()
  private context: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private worklet: AudioWorkletNode | null = null
  private silenceGain: GainNode | null = null
  private lifecycle = 0
  private startPromise: Promise<void> | null = null

  constructor(
    callbacks: LiveAudioCaptureCallbacks,
    dependencies: CaptureDependencies = {},
  ) {
    this.callbacks = callbacks
    this.dependencies = dependencies
  }

  get isActive() {
    return this.stream !== null && this.context?.state !== 'closed'
  }

  start(): Promise<void> {
    if (this.isActive) return Promise.resolve()
    if (this.startPromise) return this.startPromise
    this.vad.reset()
    const lifecycle = ++this.lifecycle
    const pending = this.performStart(lifecycle)
    const tracked = pending.finally(() => {
      if (this.startPromise === tracked) this.startPromise = null
    })
    this.startPromise = tracked
    return tracked
  }

  async close() {
    this.lifecycle += 1
    const pending = this.startPromise
    const stream = this.stream
    const context = this.context
    const source = this.source
    const worklet = this.worklet
    const silenceGain = this.silenceGain
    this.context = null
    this.stream = null
    this.source = null
    this.worklet = null
    this.silenceGain = null
    await this.dispose(stream, context, source, worklet, silenceGain)
    if (pending) await pending.catch(() => undefined)
  }

  private async performStart(lifecycle: number) {
    const mediaDevices = this.dependencies.mediaDevices ?? navigator.mediaDevices
    if (!mediaDevices?.getUserMedia) {
      throw new LiveAudioCaptureError('capture_unavailable', '当前浏览器不能使用麦克风')
    }

    let stream: MediaStream
    try {
      stream = await mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'NotAllowedError') {
        throw new LiveAudioCaptureError('permission_denied', '没有获得麦克风权限')
      }
      throw new LiveAudioCaptureError('capture_unavailable', '麦克风暂时无法使用')
    }
    if (lifecycle !== this.lifecycle) {
      stream.getTracks().forEach((track) => track.stop())
      return
    }

    let context: AudioContext | null = null
    let source: MediaStreamAudioSourceNode | null = null
    let worklet: AudioWorkletNode | null = null
    let silenceGain: GainNode | null = null
    try {
      context = this.dependencies.createAudioContext?.() ??
        new AudioContext({ latencyHint: 'interactive' })
      if (context.state === 'closed') {
        throw new LiveAudioCaptureError('audio_context_closed', '音频环境已经关闭')
      }
      if (context.state === 'suspended') await context.resume()
      await context.audioWorklet.addModule(
        this.dependencies.workletModuleUrl ?? workletModuleUrl,
      )
      if (lifecycle !== this.lifecycle) {
        await this.dispose(stream, context, source, worklet, silenceGain)
        return
      }
      source = context.createMediaStreamSource(stream)
      worklet = this.dependencies.createWorkletNode?.(context) ??
        new AudioWorkletNode(context, 'psych-pcm-capture', {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
        })
      silenceGain = context.createGain()
      silenceGain.gain.value = 0
      worklet.port.onmessage = (event: MessageEvent) => this.handleMessage(event)
      source.connect(worklet)
      worklet.connect(silenceGain)
      silenceGain.connect(context.destination)
      this.context = context
      this.stream = stream
      this.source = source
      this.worklet = worklet
      this.silenceGain = silenceGain
      for (const track of stream.getTracks()) {
        track.addEventListener('ended', () => {
          if (lifecycle !== this.lifecycle || this.stream !== stream) return
          this.callbacks.onError?.(
            new LiveAudioCaptureError('microphone_ended', '麦克风连接已经中断'),
          )
          void this.close()
        })
      }
    } catch (error) {
      await this.dispose(stream, context, source, worklet, silenceGain)
      if (error instanceof LiveAudioCaptureError) throw error
      throw new LiveAudioCaptureError('capture_unavailable', '无法启动实时音频采集')
    }
  }

  private async dispose(
    stream: MediaStream | null,
    context: AudioContext | null,
    source: MediaStreamAudioSourceNode | null,
    worklet: AudioWorkletNode | null,
    silenceGain: GainNode | null,
  ) {
    if (worklet) worklet.port.onmessage = null
    source?.disconnect()
    worklet?.disconnect()
    silenceGain?.disconnect()
    stream?.getTracks().forEach((track) => track.stop())
    if (context && context.state !== 'closed') await context.close()
  }

  private handleMessage(event: MessageEvent) {
    const data = event.data as { type?: string; pcm?: ArrayBuffer; rms?: number; atMs?: number }
    if (data.type === 'pcm' && data.pcm) {
      this.callbacks.onPcmFrame(data.pcm)
      return
    }
    if (data.type !== 'energy' || data.rms === undefined || data.atMs === undefined) return
    this.callbacks.onEnergy(data.rms)
    const candidate = this.vad.observe(data.rms, data.atMs)
    if (candidate) this.callbacks.onVadCandidate(candidate)
  }
}
