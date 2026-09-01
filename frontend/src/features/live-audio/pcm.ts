export const CAPTURE_SAMPLE_RATE = 16_000
export const CAPTURE_FRAME_SAMPLES = 320

const floatToPcm16 = (value: number) => {
  const clamped = Math.max(-1, Math.min(1, value))
  return clamped < 0 ? Math.round(clamped * 32_768) : Math.round(clamped * 32_767)
}

export const calculateRms = (samples: Float32Array) => {
  if (samples.length === 0) return 0
  let sum = 0
  for (const sample of samples) sum += sample * sample
  return Math.sqrt(sum / samples.length)
}

class StreamingResampler {
  private readonly inputSampleRate: number
  private emittedSamples = 0
  private receivedSamples = 0
  private tailStart = 0
  private tail = new Float32Array(0)

  constructor(inputSampleRate: number) {
    if (inputSampleRate < CAPTURE_SAMPLE_RATE) {
      throw new Error('麦克风采样率低于 16 kHz，无法用于实时测评')
    }
    this.inputSampleRate = inputSampleRate
  }

  push(input: Float32Array) {
    if (input.length === 0) return new Int16Array(0)
    const samples = new Float32Array(this.tail.length + input.length)
    samples.set(this.tail)
    samples.set(input, this.tail.length)
    const samplesStart = this.tailStart
    this.receivedSamples += input.length
    const output: number[] = []

    while (true) {
      const position = this.emittedSamples * this.inputSampleRate / CAPTURE_SAMPLE_RATE
      const leftGlobal = Math.floor(position)
      const fraction = position - leftGlobal
      const needsRightSample = fraction > Number.EPSILON
      if (
        leftGlobal >= this.receivedSamples ||
        (needsRightSample && leftGlobal + 1 >= this.receivedSamples)
      ) break
      const left = leftGlobal - samplesStart
      const right = needsRightSample ? left + 1 : left
      const value = samples[left] + (samples[right] - samples[left]) * fraction
      output.push(floatToPcm16(value))
      this.emittedSamples += 1
    }

    const nextPosition = this.emittedSamples * this.inputSampleRate / CAPTURE_SAMPLE_RATE
    const consumed = Math.max(
      0,
      Math.min(Math.floor(nextPosition) - samplesStart, samples.length),
    )
    this.tail = samples.slice(consumed)
    this.tailStart = samplesStart + consumed
    return Int16Array.from(output)
  }
}

export const pcm16ToLittleEndianBuffer = (samples: Int16Array) => {
  const buffer = new ArrayBuffer(samples.length * 2)
  const view = new DataView(buffer)
  samples.forEach((sample, index) => view.setInt16(index * 2, sample, true))
  return buffer
}

export class PcmFrameEncoder {
  private readonly resampler: StreamingResampler
  private pending = new Int16Array(0)

  constructor(inputSampleRate: number) {
    this.resampler = new StreamingResampler(inputSampleRate)
  }

  push(input: Float32Array) {
    const converted = this.resampler.push(input)
    if (converted.length === 0) return []
    const combined = new Int16Array(this.pending.length + converted.length)
    combined.set(this.pending)
    combined.set(converted, this.pending.length)
    const frames: Int16Array[] = []
    let offset = 0
    while (combined.length - offset >= CAPTURE_FRAME_SAMPLES) {
      frames.push(combined.slice(offset, offset + CAPTURE_FRAME_SAMPLES))
      offset += CAPTURE_FRAME_SAMPLES
    }
    this.pending = combined.slice(offset)
    return frames
  }
}
