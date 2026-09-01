import { calculateRms, pcm16ToLittleEndianBuffer, PcmFrameEncoder } from './pcm'

declare const currentTime: number
declare const sampleRate: number

declare class AudioWorkletProcessor {
  readonly port: MessagePort
}

declare function registerProcessor(
  name: string,
  processor: new () => AudioWorkletProcessor,
): void

class PcmCaptureProcessor extends AudioWorkletProcessor {
  private readonly encoder = new PcmFrameEncoder(sampleRate)

  process(inputs: Float32Array[][]) {
    const channel = inputs[0]?.[0]
    if (!channel || channel.length === 0) return true

    this.port.postMessage({
      type: 'energy',
      rms: calculateRms(channel),
      atMs: currentTime * 1000,
    })
    for (const frame of this.encoder.push(channel)) {
      const pcm = pcm16ToLittleEndianBuffer(frame)
      this.port.postMessage({ type: 'pcm', pcm }, [pcm])
    }
    return true
  }
}

registerProcessor('psych-pcm-capture', PcmCaptureProcessor)
