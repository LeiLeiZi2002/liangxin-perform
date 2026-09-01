import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from './AppShell'

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getProviderConfig: vi.fn(),
}))
vi.mock('../api/client', () => api)

function renderShell() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/test-page']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/test-page" element={<p>测试页面内容</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('应用外壳', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getHealth.mockResolvedValue({
      status: 'ready',
      service: 'psych-assessment-demo',
    })
    api.getProviderConfig.mockResolvedValue({
      configured: true,
      masked_key: 'sk-****',
      workspace_id: null,
      report_model: 'report-model',
      actor_model: 'actor-model',
      asr_model: 'asr-model',
      tts_model: 'tts-model',
      tts_voice: 'default-voice',
      report_temperature: 0.2,
      actor_temperature: 0.7,
    })
  })

  it('使用自然中文说明产品、版本身份与使用范围', async () => {
    renderShell()

    expect(screen.getByText('测试页面内容')).toBeInTheDocument()
    expect(screen.getByText('初阶心理服务从业者 · 胜任力测评')).toBeInTheDocument()
    expect(screen.queryByText('热线心理支持职业能力测评')).not.toBeInTheDocument()
    expect(screen.getByText('量心队 · 测评工作台')).toBeInTheDocument()
    expect(screen.getByText('本系统用于竞赛展示与发展性反馈')).toBeInTheDocument()
    expect(await screen.findByText('已连接')).toBeInTheDocument()
    expect(await screen.findByText('已配置')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/DEMO|PSYCHOLOGICAL|演示版本/)
  })

  it('按测评流程提供完整量规入口', () => {
    renderShell()

    const navigation = screen.getByRole('navigation', { name: '主要导航' })
    const links = within(navigation).getAllByRole('link')

    expect(links.map((link) => link.textContent)).toEqual([
      '总览',
      '正式测评',
      '自由体验',
      '任务配置',
      '完整量规',
    ])
    expect(screen.getByRole('link', { name: '完整量规' })).toHaveAttribute('href', '/rubric')
  })
})
