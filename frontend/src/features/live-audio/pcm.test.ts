import { describe, expect, it } from 'vitest'

import { pcm16ToLittleEndianBuffer, PcmFrameEncoder } from './pcm'

const flattenFrames = (frames: Int16Array[]) => {
  const output = new Int16Array(frames.length * 320)
  frames.forEach((frame, index) => output.set(frame, index * 320))
  return output
}

const encodeInChunks = (input: Float32Array, chunkSizes: number[]) => {
  const encoder = new PcmFrameEncoder(44_100)
  const frames: Int16Array[] = []
  let offset = 0
  let chunkIndex = 0
  while (offset < input.length) {
    const size = chunkSizes[chunkIndex % chunkSizes.length]
    frames.push(...encoder.push(input.subarray(offset, Math.min(input.length, offset + size))))
    offset += size
    chunkIndex += 1
  }
  return flattenFrames(frames)
}

describe('PcmFrameEncoder', () => {
  it('把 48 kHz 的 20 毫秒输入转换为一帧 16 kHz PCM16', () => {
    const encoder = new PcmFrameEncoder(48_000)
    const input = new Float32Array(960).fill(0.5)

    const frames = encoder.push(input)

    expect(frames).toHaveLength(1)
    expect(frames[0]).toBeInstanceOf(Int16Array)
    expect(frames[0]).toHaveLength(320)
    expect(frames[0][0]).toBe(16_384)
  })

  it('跨浏览器音频块保留余量并始终输出 320 个采样点', () => {
    const encoder = new PcmFrameEncoder(48_000)

    const first = encoder.push(new Float32Array(480).fill(-1))
    const second = encoder.push(new Float32Array(480).fill(1))

    expect(first).toEqual([])
    expect(second).toHaveLength(1)
    expect(second[0]).toHaveLength(320)
    expect(second[0][0]).toBe(-32_768)
    expect(second[0][319]).toBe(32_767)
  })

  it('44.1 kHz 分块输入和整段输入得到相同的插值结果', () => {
    const input = Float32Array.from(
      { length: 44_100 },
      (_, index) => Math.sin((index / 44_100) * Math.PI * 2 * 317) * 0.7,
    )

    const whole = encodeInChunks(input, [input.length])
    const chunked = encodeInChunks(input, [127, 503, 89, 1024, 251])

    expect(chunked).toEqual(whole)
    expect(chunked).toHaveLength(16_000)
  })

  it('44.1 kHz 连续十秒不会累计采样点漂移', () => {
    const input = new Float32Array(441_000)

    const output = encodeInChunks(input, [128])

    expect(output).toHaveLength(160_000)
  })

  it('明确把 PCM16 帧编码为小端字节', () => {
    const bytes = new Uint8Array(pcm16ToLittleEndianBuffer(new Int16Array([0x1234, -2])))

    expect([...bytes]).toEqual([0x34, 0x12, 0xfe, 0xff])
  })
})
