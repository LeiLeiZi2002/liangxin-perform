import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { DeviceCheckPage } from './DeviceCheckPage'

const configured = {
  configured: true,
  masked_key: '••••9zK2',
  workspace_id: null,
  report_model: 'qwen-max-report',
  actor_model: 'qwen-plus-character',
  asr_model: 'qwen-audio-3.0-asr-flash-streaming',
  tts_model: 'qwen-audio-3.0-tts-plus',
  tts_voice: 'longantingxin',
  report_temperature: 0.2,
  actor_temperature: 0.75,
  actor_context_window_tokens: 32768,
  actor_max_output_tokens: 2048,
}

function CurrentLocation() {
  const location = useLocation()
  return <output aria-label="当前地址">{location.pathname}{location.search}</output>
}

const realtimeMount = vi.fn()

function RealtimeMountProbe() {
  realtimeMount()
  return <CurrentLocation />
}

function renderPage(
  entry: string,
  options: { configured?: boolean; backendAvailable?: boolean } = {},
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (options.backendAvailable === false) throw new TypeError('connection refused')
    if (url === '/api/health') return new Response(JSON.stringify({
      status: 'ready', service: 'psych-assessment-demo',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    if (url === '/api/provider-config') return new Response(JSON.stringify({
      ...configured, configured: options.configured ?? true,
      masked_key: options.configured === false ? null : configured.masked_key,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    throw new Error(`不应调用付费检查接口：${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/device-check" element={<DeviceCheckPage />} />
          <Route path="/session/:sessionId" element={<RealtimeMountProbe />} />
          <Route path="/configure" element={<div>模型设置页面</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return fetchMock
}

describe('DeviceCheckPage', () => {
  const trackStop = vi.fn()
  const getUserMedia = vi.fn()
  const toneStart = vi.fn()
  const toneStop = vi.fn()
  const closeTone = vi.fn()

  beforeEach(() => {
    realtimeMount.mockReset()
    trackStop.mockReset()
    getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [{ stop: trackStop }] })
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    })
    toneStart.mockReset()
    toneStop.mockReset()
    closeTone.mockReset().mockResolvedValue(undefined)
    const gain = {
      value: 0,
      setValueAtTime: vi.fn(),
      linearRampToValueAtTime: vi.fn(),
    }
    vi.stubGlobal('AudioContext', vi.fn(function FakeAudioContext() {
      return {
        currentTime: 0,
        destination: {},
        createGain: () => ({ gain, connect: vi.fn(), disconnect: vi.fn() }),
        createOscillator: () => ({
          type: 'sine', frequency: { value: 0 }, connect: vi.fn(), disconnect: vi.fn(),
          start: toneStart, stop: toneStop,
        }),
        resume: vi.fn().mockResolvedValue(undefined),
        close: closeTone,
      }
    }))
  })

  it('正式热线只检查本地服务、已配置凭证和麦克风，不调用付费自检', async () => {
    const user = userEvent.setup()
    const fetchMock = renderPage(
      '/device-check?mode=assessment&scene=hotline&caseType=main&sessionId=s1',
    )

    await user.click(screen.getByRole('button', { name: '开始检查' }))

    expect(getUserMedia).toHaveBeenCalledWith(expect.objectContaining({ audio: expect.any(Object) }))
    expect(trackStop).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/health', undefined)
    expect(fetchMock).toHaveBeenCalledWith('/api/provider-config', undefined)
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/provider-config/check')).toBe(false)
    expect(await screen.findAllByText('已就绪')).toHaveLength(3)
    expect(realtimeMount).not.toHaveBeenCalled()
    expect(screen.getByText('匿名来电')).toBeInTheDocument()
    expect(screen.getByText('正在呼入…')).toBeInTheDocument()
    await waitFor(() => expect(toneStart).toHaveBeenCalledTimes(2))
    const answer = screen.getByRole('button', { name: '接听来电' })
    expect(answer).toBeEnabled()

    await user.click(answer)

    expect(screen.getByRole('button', { name: '正在接听…' })).toBeDisabled()
    expect(realtimeMount).not.toHaveBeenCalled()
    await waitFor(() => expect(closeTone).toHaveBeenCalledOnce())
    expect(await screen.findByLabelText('当前地址')).toHaveTextContent(
      '/session/s1?mode=assessment&scene=hotline&caseType=main',
    )
    expect(realtimeMount).toHaveBeenCalledOnce()
  })

  it('没有配置 Key 时说明原因并提供设置入口', async () => {
    const user = userEvent.setup()
    renderPage('/device-check?mode=assessment&scene=hotline&sessionId=s1', { configured: false })

    await user.click(screen.getByRole('button', { name: '开始检查' }))

    expect(await screen.findByText('尚未配置模型调用凭证')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '前往设置' })).toHaveAttribute('href', '/configure')
    expect(screen.getByRole('button', { name: '接听来电' })).toBeDisabled()
  })

  it('麦克风权限被拒绝时给出可执行提示并允许重新检查', async () => {
    getUserMedia.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'))
    const user = userEvent.setup()
    renderPage('/device-check?mode=assessment&scene=hotline&sessionId=s1')

    await user.click(screen.getByRole('button', { name: '开始检查' }))

    expect(await screen.findByText('请允许浏览器使用麦克风')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重新检查' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '接听来电' })).toBeDisabled()
  })

  it('本地后端不可用时直接说明启动方式，不误报模型故障', async () => {
    const user = userEvent.setup()
    renderPage('/device-check?mode=assessment&scene=hotline&sessionId=s1', {
      backendAvailable: false,
    })

    await user.click(screen.getByRole('button', { name: '开始检查' }))

    expect(await screen.findByText('本地服务暂时没有回应')).toBeInTheDocument()
    expect(screen.getByText(/启动DEMO\.cmd/)).toBeInTheDocument()
    expect(screen.queryByText(/Director|Actor|语音合成模型/)).not.toBeInTheDocument()
  })

  it('文字体验不请求麦克风，只检查本地服务和凭证', async () => {
    const user = userEvent.setup()
    renderPage('/device-check?mode=experience&scene=online&caseType=short&sessionId=s2')

    await user.click(screen.getByRole('button', { name: '开始检查' }))

    expect(getUserMedia).not.toHaveBeenCalled()
    expect(await screen.findAllByText('已就绪')).toHaveLength(2)
    expect(screen.getByRole('button', { name: '进入会谈' })).toBeEnabled()
  })
})
