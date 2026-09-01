import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { StartPage } from './StartPage'

const now = '2026-08-27T00:00:00Z'

function json(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('StartPage', () => {
  it('正式测评先读取管理配置，再按配置抽取个案并建立会话', async () => {
    const session = {
      id: 'created-session', mode: 'assessment', scene: 'online', case_type: 'main',
      case_id: 'marriage_boundary_main', media: 'text', status: 'active',
      model_mode: 'live', soft_duration_minutes: null, created_at: now, updated_at: now,
      ended_at: null, end_reason: null,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/demo-config') return json({
        scene: 'online', case_type: 'main', task_count: 1,
        soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
      })
      if (url === '/api/cases/draw') return json({
        case_id: 'marriage_boundary_main',
        title: '锁屏亮了一下', case_type: 'main',
        public_entry: {
          role: '在线支持工作者',
          known_information: ['当前收到一条咨询消息'],
          task_boundary: ['通过自然交流开展工作'],
        },
        estimated_duration_minutes: 20, scene: 'online', media: 'text',
        available_scenes: ['hotline', 'online'],
      })
      if (url === '/api/sessions') return json(session)
      if (url === '/api/sessions/created-session/continue') throw new Error('不应提前生成开场')
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const user = userEvent.setup()

    render(
      <StrictMode>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/assessment']}>
            <Routes>
              <Route path="/assessment" element={<StartPage mode="assessment" />} />
              <Route path="/device-check" element={<CurrentLocation />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      </StrictMode>,
    )

    expect(await screen.findByText('在线咨询 · 主个案')).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
    expect(screen.queryByText('锁屏亮了一下')).not.toBeInTheDocument()
    expect(screen.queryAllByText(/风险|边界|转介/)).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: '开始正式测评' }))

    expect(await screen.findByLabelText('当前地址')).toHaveTextContent(
      '/device-check?mode=assessment&scene=online&caseType=main&sessionId=created-session',
    )
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/cases/draw', expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ scene: 'online', case_type: 'main', excluded_case_ids: [] }),
      }))
      expect(fetchMock).toHaveBeenCalledWith('/api/sessions', expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          mode: 'assessment', scene: 'online', case_type: 'main', case_id: 'marriage_boundary_main',
        }),
      }))
      expect(fetchMock.mock.calls.map(([url]) => String(url)).slice(0, 3)).toEqual([
        '/api/demo-config',
        '/api/cases/draw',
        '/api/sessions',
      ])
      expect(fetchMock.mock.calls.filter(([url]) => String(url) === '/api/sessions')).toHaveLength(1)
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/continue'))).toBe(false)
    })
  })

  it('正式测评刷新缓存配置期间不允许按旧配置开始', async () => {
    let resolveConfig!: (response: Response) => void
    const configResponse = new Promise<Response>((resolve) => {
      resolveConfig = resolve
    })
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === '/api/demo-config') return configResponse
      throw new Error(`unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    queryClient.setQueryData(['demo-config'], {
      scene: 'hotline', case_type: 'short', task_count: 1,
      soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <StartPage mode="assessment" />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByRole('button', { name: '开始正式测评' })).toBeDisabled()
    expect(screen.getByText('正在读取本次测评配置…')).toBeInTheDocument()

    resolveConfig(json({
      scene: 'online', case_type: 'main', task_count: 1,
      soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
    }))
    expect(await screen.findByText('在线咨询 · 主个案')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始正式测评' })).toBeEnabled()
  })

  it('正式测评遇到配置冲突时只刷新配置，再由用户按新配置重新开始', async () => {
    let configRequestCount = 0
    let sessionRequestCount = 0
    let resolveRefreshedConfig!: (response: Response) => void
    const refreshedConfig = new Promise<Response>((resolve) => {
      resolveRefreshedConfig = resolve
    })
    const drawBodies: unknown[] = []
    const sessionBodies: unknown[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url === '/api/demo-config') {
        configRequestCount += 1
        if (configRequestCount === 1) return json({
          scene: 'hotline', case_type: 'main', task_count: 1,
          soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
        })
        return refreshedConfig
      }
      if (url === '/api/cases/draw') {
        drawBodies.push(JSON.parse(init?.body as string))
        return json({
          case_id: 'marriage_boundary_main', title: '锁屏亮了一下', case_type: 'main',
          public_entry: { role: '支持工作者', known_information: [], task_boundary: [] },
          estimated_duration_minutes: 20,
          scene: drawBodies.length === 1 ? 'hotline' : 'online',
          media: drawBodies.length === 1 ? 'voice' : 'text',
          available_scenes: ['hotline', 'online'],
        })
      }
      if (url === '/api/sessions') {
        sessionRequestCount += 1
        sessionBodies.push(JSON.parse(init?.body as string))
        if (sessionRequestCount === 1) {
          return new Response(JSON.stringify({
            detail: '正式测评场域与当前管理配置不一致',
          }), {
            status: 409,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return json({
          id: 'refreshed-session', mode: 'assessment', scene: 'online', case_type: 'main',
          case_id: 'marriage_boundary_main', media: 'text', status: 'active',
          model_mode: 'live', soft_duration_minutes: null, created_at: now, updated_at: now,
          ended_at: null, end_reason: null,
        })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(
      <QueryClientProvider client={new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })}>
        <MemoryRouter initialEntries={['/assessment']}>
          <Routes>
            <Route path="/assessment" element={<StartPage mode="assessment" />} />
            <Route path="/device-check" element={<CurrentLocation />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('心理热线 · 主个案')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始正式测评' }))

    expect(await screen.findByText('管理配置已更新，正在重新读取')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '正在抽取并建立会话…' })).toBeDisabled()
    expect(drawBodies).toEqual([
      { scene: 'hotline', case_type: 'main', excluded_case_ids: [] },
    ])
    expect(sessionBodies).toHaveLength(1)

    resolveRefreshedConfig(json({
      scene: 'online', case_type: 'main', task_count: 1,
      soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
    }))
    expect(await screen.findByText('在线咨询 · 主个案')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '开始正式测评' })).toBeEnabled()
    })
    expect(sessionBodies).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: '开始正式测评' }))
    expect(await screen.findByLabelText('当前地址')).toHaveTextContent(
      '/device-check?mode=assessment&scene=online&caseType=main&sessionId=refreshed-session',
    )
    expect(drawBodies).toEqual([
      { scene: 'hotline', case_type: 'main', excluded_case_ids: [] },
      { scene: 'online', case_type: 'main', excluded_case_ids: [] },
    ])
    expect(sessionBodies).toEqual([
      {
        mode: 'assessment', scene: 'hotline', case_type: 'main',
        case_id: 'marriage_boundary_main',
      },
      {
        mode: 'assessment', scene: 'online', case_type: 'main',
        case_id: 'marriage_boundary_main',
      },
    ])
  })

  it('非配置冲突错误显示后端具体说明且不重读配置', async () => {
    let configRequestCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/demo-config') {
        configRequestCount += 1
        return json({
          scene: 'hotline', case_type: 'main', task_count: 1,
          soft_duration_minutes: null, model_mode: 'live', require_work_record: true,
        })
      }
      if (url === '/api/cases/draw') return json({
        case_id: 'marriage_boundary_main', title: '锁屏亮了一下', case_type: 'main',
        public_entry: { role: '支持工作者', known_information: [], task_boundary: [] },
        estimated_duration_minutes: 20, scene: 'hotline', media: 'voice',
        available_scenes: ['hotline', 'online'],
      })
      if (url === '/api/sessions') {
        return new Response(JSON.stringify({ detail: '个案暂时不可用，请稍后重试' }), {
          status: 422,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })}>
        <MemoryRouter><StartPage mode="assessment" /></MemoryRouter>
      </QueryClientProvider>,
    )

    await screen.findByText('心理热线 · 主个案')
    await user.click(screen.getByRole('button', { name: '开始正式测评' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('个案暂时不可用，请稍后重试')
    expect(configRequestCount).toBe(1)
  })

  it('自由体验禁用机构面谈，仍可选择已开放场域和个案类型', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/cases/draw') return json({
        case_id: 'short-online', title: '体验个案', case_type: 'short',
        public_entry: { role: '线上支持工作者', known_information: [], task_boundary: [] },
        estimated_duration_minutes: 10, scene: 'online', media: 'text',
        available_scenes: ['online'],
      })
      if (url === '/api/sessions') return json({
        id: 'experience-session', mode: 'experience', scene: 'online', case_type: 'short',
        case_id: 'short-online', media: 'text', status: 'active', model_mode: 'fallback',
        soft_duration_minutes: null, created_at: now, updated_at: now,
        ended_at: null, end_reason: null,
      })
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <MemoryRouter initialEntries={['/experience']}>
          <Routes>
            <Route path="/experience" element={<StartPage mode="experience" />} />
            <Route path="/device-check" element={<CurrentLocation />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const institutionOption = screen.getByRole('radio', { name: '机构面谈' })
    expect(institutionOption).toBeDisabled()
    expect(institutionOption).toHaveAccessibleDescription('DEMO 暂未开放')
    expect(institutionOption.closest('label')).toHaveTextContent('DEMO 暂未开放')
    expect(screen.getByRole('radio', { name: '心理热线' })).toBeEnabled()
    expect(screen.getByRole('radio', { name: '在线咨询' })).toBeEnabled()

    await user.click(screen.getByRole('radio', { name: '在线咨询' }))
    expect(screen.getByRole('radio', { name: '主个案' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: '短个案' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: '抽取个案并开始' }))

    expect(await screen.findByLabelText('当前地址')).toHaveTextContent(
      '/device-check?mode=experience&scene=online&caseType=short&sessionId=experience-session',
    )
    expect(fetchMock).toHaveBeenCalledWith('/api/cases/draw', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ scene: 'online', case_type: 'short', excluded_case_ids: [] }),
    }))
    expect(fetchMock.mock.calls.some(([url]) => String(url) === '/api/demo-config')).toBe(false)
  })

  it('建立会话期间锁定选择，并用提交快照进入匹配的设备检查', async () => {
    const session = {
      id: 'pending-session', mode: 'experience', scene: 'online', case_type: 'short',
      case_id: 'short-online', media: 'text', status: 'active', model_mode: 'fallback',
      soft_duration_minutes: null, created_at: now, updated_at: now,
      ended_at: null, end_reason: null,
    }
    let resolveSession!: (response: Response) => void
    const sessionResponse = new Promise<Response>((resolve) => {
      resolveSession = resolve
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/cases/draw') return json({
        case_id: 'short-online', title: '体验个案', case_type: 'short',
        public_entry: { role: '线上支持工作者', known_information: [], task_boundary: [] },
        estimated_duration_minutes: 10, scene: 'online', media: 'text',
        available_scenes: ['online'],
      })
      if (url === '/api/sessions') return sessionResponse
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const page = (pageMode: 'assessment' | 'experience') => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<StartPage mode={pageMode} />} />
            <Route path="/device-check" element={<CurrentLocation />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    const view = render(page('experience'))

    await user.click(screen.getByRole('radio', { name: '在线咨询' }))
    await user.click(screen.getByRole('button', { name: '抽取个案并开始' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/sessions', expect.anything()))

    for (const option of screen.getAllByRole('radio')) {
      expect(option).toBeDisabled()
    }

    view.rerender(page('assessment'))
    resolveSession(json(session))

    expect(await screen.findByLabelText('当前地址')).toHaveTextContent(
      '/device-check?mode=experience&scene=online&caseType=short&sessionId=pending-session',
    )
  })
})

function CurrentLocation() {
  const location = useLocation()
  return <output aria-label="当前地址">{location.pathname}{location.search}</output>
}
