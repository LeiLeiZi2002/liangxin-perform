import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfigurePage } from './ConfigurePage'

const initialConfig = {
  scene: 'hotline',
  case_type: 'main',
  task_count: 1,
  soft_duration_minutes: 15,
  model_mode: 'live',
  require_work_record: true,
}

const initialProviderConfig = {
  configured: false,
  masked_key: null,
  workspace_id: null,
  report_model: 'qwen-max-report',
  actor_model: 'qwen-plus-character',
  asr_model: 'qwen-audio-3.0-asr-flash-streaming',
  tts_model: 'qwen-audio-3.0-tts-plus',
  tts_voice: 'longanlingxin',
  report_temperature: 0.2,
  actor_temperature: 0.75,
  actor_context_window_tokens: 32768,
  actor_max_output_tokens: 2048,
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ConfigurePage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function expectFormControlsDisabled(form: HTMLFormElement) {
  const controls = form.querySelectorAll('input, select, textarea, button')
  expect(controls.length).toBeGreaterThan(0)
  controls.forEach((control) => expect(control).toBeDisabled())
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('任务配置', () => {
  it('提交百炼密钥时仅保存在页面状态并发送服务配置', async () => {
    const providerConfig = {
      configured: true,
      masked_key: '••••1234',
      workspace_id: null,
      report_model: 'qwen-max-report',
      actor_model: 'qwen-plus-character',
      asr_model: 'qwen-audio-3.0-asr-flash-streaming',
      tts_model: 'qwen-audio-3.0-tts-plus',
      tts_voice: 'longanlingxin',
      report_temperature: 0.2,
      actor_temperature: 0.75,
      actor_context_window_tokens: 32768,
      actor_max_output_tokens: 2048,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/demo-config') {
        return new Response(JSON.stringify(initialConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (path === '/api/provider-config' && init?.method === 'PUT') {
        return new Response(JSON.stringify({ ...providerConfig, workspace_id: 'workspace-123' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(providerConfig), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem')
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    expect(screen.getByText('启动器会优先载入本机用户环境中的密钥；在此填写的密钥只保留到当前后端进程结束。')).toBeInTheDocument()
    expect(await screen.findByText('已配置 · 末四位 1234')).toBeInTheDocument()
    await user.click(screen.getByText('高级设置'))
    expect(screen.getByLabelText('报告分析模型')).toHaveValue('qwen-max-report')
    expect(screen.getByLabelText('报告分析温度')).toHaveValue(0.2)
    expect(screen.getByLabelText('来访者对话模型')).toHaveValue('qwen-plus-character')
    expect(screen.getByLabelText('来访者对话温度')).toHaveValue(0.75)
    expect(screen.getByLabelText('对话模型上下文容量')).toHaveValue(32768)
    expect(screen.getByLabelText('单次回复输出上限')).toHaveValue(2048)
    expect(screen.getByText('容量接近上限时，来访者会先聚焦当前话题，再自然收束会话。')).toBeInTheDocument()
    expect(screen.queryByText(/Director|Actor/)).not.toBeInTheDocument()
    await user.clear(screen.getByLabelText('报告分析模型'))
    await user.type(screen.getByLabelText('报告分析模型'), 'qwen-plus-report')
    await user.clear(screen.getByLabelText('报告分析温度'))
    await user.type(screen.getByLabelText('报告分析温度'), '0.35')
    await user.clear(screen.getByLabelText('对话模型上下文容量'))
    await user.type(screen.getByLabelText('对话模型上下文容量'), '24000')
    await user.clear(screen.getByLabelText('单次回复输出上限'))
    await user.type(screen.getByLabelText('单次回复输出上限'), '3072')
    await user.type(screen.getByLabelText('百炼 API Key'), 'test-never-store-me-1234')
    await user.type(screen.getByLabelText('业务空间标识（可选）'), 'workspace-123')
    await user.click(screen.getByRole('button', { name: '保存服务配置' }))

    await screen.findByText('模型与语音服务配置已保存。')
    const request = fetchMock.mock.calls.find(
      ([path, init]) => path === '/api/provider-config' && init?.method === 'PUT',
    )
    expect(request).toBeDefined()
    const payload = JSON.parse(request?.[1]?.body as string)
    expect(payload).toMatchObject({
      api_key: 'test-never-store-me-1234',
      workspace_id: 'workspace-123',
      report_model: 'qwen-plus-report',
      report_temperature: 0.35,
      actor_context_window_tokens: 24000,
      actor_max_output_tokens: 3072,
    })
    expect(payload).not.toHaveProperty('director_model')
    expect(payload).not.toHaveProperty('director_temperature')
    expect(storageSpy).not.toHaveBeenCalled()
    expect(screen.getByLabelText('百炼 API Key')).toHaveValue('')
  })

  it('从后端读取并完整回填当前配置', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) =>
        Promise.resolve(new Response(JSON.stringify(
          String(input) === '/api/provider-config' ? initialProviderConfig : initialConfig,
        ), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })),
      ),
    )

    renderPage()

    expect(screen.getByText('正在读取任务配置…')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '任务配置' })).toBeInTheDocument()
    const sceneGroup = screen.getByRole('radiogroup', { name: '指定测评场域' })
    const institutionOption = within(sceneGroup).getByRole('radio', { name: '机构面谈' })
    expect(institutionOption).toBeDisabled()
    expect(institutionOption).toHaveAccessibleDescription('实时语音 DEMO 暂未开放')
    expect(institutionOption.closest('label')).toHaveTextContent('DEMO 暂未开放')
    expect(within(sceneGroup).getByRole('radio', { name: '心理热线' })).toBeChecked()
    expect(within(sceneGroup).getByRole('radio', { name: '心理热线' })).toBeEnabled()
    expect(within(sceneGroup).getByRole('radio', { name: '在线咨询' })).toBeEnabled()
    expect(screen.getByLabelText('个案类型')).toHaveValue('main')
    expect(screen.getByLabelText('任务数量')).toHaveValue(1)
    expect(screen.getByLabelText('任务数量')).toBeDisabled()
    expect(screen.getByText('当前演示每次安排一份任务。')).toBeInTheDocument()
    expect(screen.queryByLabelText('软时间范围（分钟）')).not.toBeInTheDocument()
    expect(screen.queryByText(/软时间/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('模型模式')).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /必须填写工作记录/ })).toBeChecked()
    expect(screen.queryByText(/首批题库|隐匿危机风险|专业边界与转介/)).not.toBeInTheDocument()
  })

  it('切换到未知来访者模型时不沿用旧容量', async () => {
    const putPayloads: Array<Record<string, unknown>> = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/provider-config' && init?.method === 'PUT') {
        const payload = JSON.parse(init.body as string) as Record<string, unknown>
        putPayloads.push(payload)
        const publicPayload = { ...payload }
        delete publicPayload.api_key
        return new Response(JSON.stringify({
          ...initialProviderConfig,
          ...publicPayload,
          configured: true,
          masked_key: '•••1234',
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(
        path === '/api/provider-config' ? initialProviderConfig : initialConfig,
      ), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    await user.click(screen.getByText('高级设置'))
    const actorModel = screen.getByLabelText('来访者对话模型')
    await user.clear(actorModel)
    await user.type(actorModel, 'private-character-model')

    expect(screen.getByLabelText('对话模型上下文容量')).toHaveValue(null)
    await user.click(screen.getByRole('button', { name: '保存服务配置' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '换用未知来访者对话模型时，请填写模型官方上下文容量。',
    )
    expect(putPayloads).toHaveLength(0)

    await user.type(screen.getByLabelText('对话模型上下文容量'), '16000')
    await user.click(screen.getByRole('button', { name: '保存服务配置' }))
    await screen.findByText('模型与语音服务配置已保存。')

    expect(putPayloads).toHaveLength(1)
    expect(putPayloads[0]).toMatchObject({
      actor_model: 'private-character-model',
      actor_context_window_tokens: 16000,
    })
    expect(putPayloads[0].actor_context_window_tokens).not.toBe(32768)
  })

  it('已保存未知模型时修改其他配置不清空已确认容量', async () => {
    const savedUnknownConfig = {
      ...initialProviderConfig,
      actor_model: 'private-character-model',
      actor_context_window_tokens: 16000,
      actor_max_output_tokens: 1024,
    }
    const putPayloads: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/provider-config' && init?.method === 'PUT') {
        const payload = JSON.parse(init.body as string) as Record<string, unknown>
        putPayloads.push(payload)
        const publicPayload = { ...payload }
        delete publicPayload.api_key
        return new Response(JSON.stringify({
          ...savedUnknownConfig,
          ...publicPayload,
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(
        path === '/api/provider-config' ? savedUnknownConfig : initialConfig,
      ), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    await user.click(screen.getByText('高级设置'))
    await user.clear(screen.getByLabelText('报告分析温度'))
    await user.type(screen.getByLabelText('报告分析温度'), '0.3')

    expect(screen.getByLabelText('对话模型上下文容量')).toHaveValue(16000)
    await user.click(screen.getByRole('button', { name: '保存服务配置' }))
    await screen.findByText('模型与语音服务配置已保存。')

    expect(putPayloads[0]).toMatchObject({
      actor_model: 'private-character-model',
      actor_context_window_tokens: 16000,
      actor_max_output_tokens: 1024,
      report_temperature: 0.3,
    })
  })

  it('切走再切回已保存的已知模型时恢复自定义容量与输出上限', async () => {
    const savedKnownCustomConfig = {
      ...initialProviderConfig,
      actor_context_window_tokens: 30000,
      actor_max_output_tokens: 1536,
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      return new Response(JSON.stringify(
        path === '/api/provider-config' ? savedKnownCustomConfig : initialConfig,
      ), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    await user.click(screen.getByText('高级设置'))
    const actorModel = screen.getByLabelText('来访者对话模型')
    await user.clear(actorModel)
    await user.type(actorModel, 'private-character-model')
    await user.clear(actorModel)
    await user.type(actorModel, 'qwen-plus-character')

    expect(screen.getByLabelText('对话模型上下文容量')).toHaveValue(30000)
    expect(screen.getByLabelText('单次回复输出上限')).toHaveValue(1536)
  })

  it('将遗留的机构配置回退到可用热线，避免再次保存不可用场域', async () => {
    const legacyConfig = { ...initialConfig, scene: 'institution' }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const data = path === '/api/provider-config'
        ? initialProviderConfig
        : init?.method === 'PUT'
          ? JSON.parse(init.body as string)
          : legacyConfig
      return new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    expect(screen.getByRole('radio', { name: /机构面谈/ })).toBeDisabled()
    expect(screen.getByRole('radio', { name: /心理热线/ })).toBeChecked()
    await user.click(screen.getByRole('button', { name: '保存配置' }))

    await screen.findByText('配置已保存，仅对新会话生效。')
    const request = fetchMock.mock.calls.find(
      ([path, init]) => path === '/api/demo-config' && init?.method === 'PUT',
    )
    expect(JSON.parse(request?.[1]?.body as string)).toMatchObject({ scene: 'hotline' })
  })

  it('两个配置表单仅在各自保存期间锁定所有控件', async () => {
    let resolveDemo!: (response: Response) => void
    let resolveProvider!: (response: Response) => void
    const demoPending = new Promise<Response>((resolve) => { resolveDemo = resolve })
    const providerPending = new Promise<Response>((resolve) => { resolveProvider = resolve })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/demo-config' && init?.method === 'PUT') return demoPending
      if (path === '/api/provider-config' && init?.method === 'PUT') return providerPending
      return Promise.resolve(new Response(JSON.stringify(
        path === '/api/provider-config' ? initialProviderConfig : initialConfig,
      ), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    const demoSave = screen.getByRole('button', { name: '保存配置' })
    const providerSave = await screen.findByRole('button', { name: '保存服务配置' })
    const demoForm = demoSave.closest('form') as HTMLFormElement
    const providerForm = providerSave.closest('form') as HTMLFormElement

    await user.click(demoSave)
    expect(await screen.findByRole('button', { name: '正在保存…' })).toBeDisabled()
    expectFormControlsDisabled(demoForm)
    expect(screen.getByLabelText('百炼 API Key')).toBeEnabled()
    resolveDemo(new Response(JSON.stringify(initialConfig), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    await screen.findByText('配置已保存，仅对新会话生效。')

    await user.click(providerSave)
    const pendingButtons = await screen.findAllByRole('button', { name: '正在保存…' })
    expect(pendingButtons).toHaveLength(1)
    expectFormControlsDisabled(providerForm)
    expect(screen.getByLabelText('个案类型')).toBeEnabled()
    resolveProvider(new Response(JSON.stringify(initialProviderConfig), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    await screen.findByText('模型与语音服务配置已保存。')
  })

  it('保存严格符合后端 DemoConfig 合同并显示成功状态', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/provider-config') {
        return new Response(JSON.stringify(initialProviderConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(init?.body as BodyInit ?? JSON.stringify(initialConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    await user.click(screen.getByRole('radio', { name: /在线咨询/ }))
    await user.selectOptions(screen.getByLabelText('个案类型'), 'short')
    expect(screen.getByLabelText('任务数量')).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /必须填写工作记录/ }))
    await user.click(screen.getByRole('button', { name: '保存配置' }))

    await screen.findByText('配置已保存，仅对新会话生效。')
    const request = fetchMock.mock.calls.find(
      ([path, init]) => path === '/api/demo-config' && init?.method === 'PUT',
    )
    expect(request).toBeDefined()
    expect(request?.[1]).toEqual(expect.objectContaining({
      method: 'PUT',
      headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    }))
    const body = JSON.parse(request?.[1]?.body as string)
    expect(body).toEqual({
      scene: 'online',
      case_type: 'short',
      task_count: 1,
      soft_duration_minutes: null,
      model_mode: 'live',
      require_work_record: false,
    })
  })

  it('显示 GET 失败并允许重新读取', async () => {
    let demoRequestCount = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/provider-config') {
        return new Response(JSON.stringify(initialProviderConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      demoRequestCount += 1
      if (demoRequestCount === 1) {
        return new Response(JSON.stringify({ detail: '服务暂时不可用' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(initialConfig), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('服务暂时不可用')
    await user.click(screen.getByRole('button', { name: '重新读取' }))
    expect(await screen.findByRole('heading', { name: '任务配置' })).toBeInTheDocument()
    expect(demoRequestCount).toBe(2)
  })

  it('显示保存失败且保留用户刚刚填写的内容', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/provider-config') {
        return new Response(JSON.stringify(initialProviderConfig), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (init?.method === 'PUT') {
        return new Response(JSON.stringify({ detail: '配置未能保存' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify(initialConfig), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    await user.selectOptions(screen.getByLabelText('个案类型'), 'short')
    await user.click(screen.getByRole('button', { name: '保存配置' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('配置未能保存')
    await waitFor(() => expect(screen.getByLabelText('个案类型')).toHaveValue('short'))
  })

  it('遗留配置数量大于一时仍固定显示并保存一份任务', async () => {
    const legacyConfig = { ...initialConfig, task_count: 4 }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      const data = path === '/api/provider-config'
        ? initialProviderConfig
        : init?.method === 'PUT'
          ? JSON.parse(init.body as string)
          : legacyConfig
      return new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('heading', { name: '任务配置' })
    expect(screen.getByLabelText('任务数量')).toHaveValue(1)
    expect(screen.getByLabelText('任务数量')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '保存配置' }))

    await screen.findByText('配置已保存，仅对新会话生效。')
    const request = fetchMock.mock.calls.find(
      ([path, init]) => path === '/api/demo-config' && init?.method === 'PUT',
    )
    expect(JSON.parse(request?.[1]?.body as string)).toMatchObject({ task_count: 1 })
  })
})
