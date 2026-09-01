import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SessionPage } from './SessionPage'

const liveMock = vi.hoisted(() => ({ current: {} as Record<string, unknown> }))
vi.mock('../features/live-session/use-live-session', async (importOriginal) => ({
  ...await importOriginal<typeof import('../features/live-session/use-live-session')>(),
  useLiveSession: () => liveMock.current,
}))

const now = '2026-08-29T00:00:00Z'

function session(scene: 'online' | 'institution' | 'hotline', mode = 'assessment') {
  return {
    id: 's1', mode, scene, case_type: 'main', case_id: 'case-1',
    media: scene === 'online' ? 'text' : 'voice', status: 'active', model_mode: 'live',
    soft_duration_minutes: 15, created_at: now, updated_at: now,
    ended_at: null, end_reason: null,
  }
}

function sessionWithSoftDuration(
  scene: 'online' | 'institution' | 'hotline',
  softDuration: number | null,
) {
  return { ...session(scene), soft_duration_minutes: softDuration }
}

const persisted = [
  { id: 'c1', sequence: 1, speaker: 'client', text: '喂，你好。', client_turn_id: 'opening' },
  { id: 'w2', sequence: 2, speaker: 'worker', text: '你好，我在听。', client_turn_id: 'turn-1' },
]

function defaultLive(overrides: Record<string, unknown> = {}) {
  return {
    connection: 'connected', phase: 'listening', transcript: persisted,
    liveTranscript: '', visitorPreview: '', visitorReveal: null,
    textTurnStatus: 'idle', technicalPause: null, inputError: '',
    retrying: false, isPlaying: false, endedReason: null, energy: 0.02,
    voiceActivity: { state: 'quiet', confirmedSilenceMs: 0 },
    canManualComplete: true,
    manualCompletePending: false,
    canRedoInput: false, redoInputPending: false, inputNotice: '',
    retry: vi.fn(), redoInput: vi.fn(), manualComplete: vi.fn(),
    sendText: vi.fn(() => true), endSession: vi.fn(),
    ...overrides,
  }
}

function json(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderSession(
  scene: 'online' | 'institution' | 'hotline',
  mode = 'assessment',
  transcript = persisted,
) {
  const restTurns = transcript.map((turn) => ({
    ...turn, provider: null, degraded: false, created_at: now, audio_available: false,
  }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
    session: session(scene, mode), transcript: restTurns,
  })))
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/session/s1']}>
        <Routes>
          <Route path="/session/:sessionId" element={<SessionPage />} />
          <Route path="/configure" element={<div>模型设置页面</div>} />
          <Route path="/sessions/:sessionId/work-record" element={<div>工作记录页面</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function renderSessionWithSoftDuration(softDuration: number | null) {
  const restTurns = persisted.map((turn) => ({
    ...turn, provider: null, degraded: false, created_at: now, audio_available: false,
  }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
    session: sessionWithSoftDuration('online', softDuration), transcript: restTurns,
  })))
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })}>
      <MemoryRouter initialEntries={['/session/s1']}>
        <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function renderPersistedEndedSession(
  reason: 'natural_closure' | 'user_ended' | 'technical_interruption',
  scene: 'online' | 'hotline' = 'hotline',
) {
  const endedSession = {
    ...session(scene),
    status: 'ended',
    ended_at: now,
    end_reason: reason,
  }
  const restTurns = persisted.map((turn) => ({
    ...turn, provider: null, degraded: false, created_at: now, audio_available: false,
  }))
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
    session: endedSession, transcript: restTurns,
  })))
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })}>
      <MemoryRouter initialEntries={['/session/s1']}>
        <Routes>
          <Route path="/session/:sessionId" element={<SessionPage />} />
          <Route path="/sessions/:sessionId/work-record" element={<div>工作记录页面</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SessionPage', () => {
  beforeEach(() => {
    liveMock.current = defaultLive()
  })

  it('桌面会谈使用左侧通话控制、右侧默认展开原文的工作台', async () => {
    liveMock.current = defaultLive({ liveTranscript: '我想先了解一下你现在', energy: 0.05 })
    const user = userEvent.setup()
    renderSession('hotline')

    const callControls = await screen.findByRole('region', { name: '通话与控制' })
    const transcript = screen.getByRole('region', { name: '会谈原文' })
    expect(callControls.parentElement).toBe(transcript.parentElement)
    expect(callControls.parentElement).toHaveClass('session-workbench-grid')
    expect(screen.getByRole('button', { name: '收起会谈原文' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('喂，你好。')).toBeInTheDocument()
    expect(screen.getByText('我想先了解一下你现在')).toBeInTheDocument()
    expect(screen.getByText('麦克风已连接')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '我说完了' })).toBeEnabled()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByText(/改用文字|文字降级/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '收起会谈原文' }))
    expect(screen.queryByText('喂，你好。')).not.toBeInTheDocument()
    expect(screen.queryByText('我想先了解一下你现在')).not.toBeInTheDocument()
  })

  it('实时转写出现后可以放弃本轮并重新说，不会直接提交旧文字', async () => {
    const redoInput = vi.fn()
    liveMock.current = defaultLive({
      liveTranscript: '关系的，没有没有关系的。',
      canRedoInput: true,
      redoInput,
    })
    const user = userEvent.setup()
    renderSession('hotline')

    expect(await screen.findByText('识别中，文字可能继续修正')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新说这句' }))

    expect(redoInput).toHaveBeenCalledOnce()
  })

  it('清空旧转写后明确提示重新说整句', async () => {
    liveMock.current = defaultLive({ inputNotice: '已清空，请重新说这一句' })
    renderSession('hotline')

    expect(await screen.findByRole('status')).toHaveTextContent('已清空，请重新说这一句')
  })

  it('语音生成和播放期间禁用“我说完了”', async () => {
    const manualComplete = vi.fn()
    liveMock.current = defaultLive({ phase: 'acting', manualComplete })
    const view = renderSession('hotline')

    expect(await screen.findByRole('button', { name: '我说完了' })).toBeDisabled()

    liveMock.current = defaultLive({ phase: 'playing', isPlaying: true, manualComplete })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('button', { name: '我说完了' })).toBeDisabled()
    expect(manualComplete).not.toHaveBeenCalled()
  })

  it('VAD 停顿只作为手动提交提示', async () => {
    liveMock.current = defaultLive({
      voiceActivity: { state: 'paused', confirmedSilenceMs: 620 },
    })
    renderSession('hotline')

    expect(await screen.findByText('检测到停顿，确认说完后请点击“我说完了”。')).toBeInTheDocument()
  })

  it('来访者播放时提示先听完，不误称正在记录受测者发言', async () => {
    liveMock.current = defaultLive({
      phase: 'playing', isPlaying: true, visitorPreview: '嗯……你先说。',
    })
    renderSession('institution')

    expect(await screen.findAllByText('来访者正在说话')).not.toHaveLength(0)
    expect(screen.getByText('嗯……你先说。')).toBeInTheDocument()
    expect(screen.getByText('来访者正在说话，请听完后再继续回应。')).toBeInTheDocument()
    expect(screen.queryByText(/记录你的自然反应/)).not.toBeInTheDocument()
  })

  it('手动提交后按钮明确显示正在提交', async () => {
    liveMock.current = defaultLive({
      canManualComplete: false,
      manualCompletePending: true,
    })
    renderSession('hotline')

    expect(await screen.findByRole('button', { name: '正在提交…' })).toBeDisabled()
  })

  it('对话处理中只呈现热线可理解的状态，不展示内部运行阶段', async () => {
    liveMock.current = defaultLive({ phase: 'directing' })
    renderSession('hotline')

    expect(await screen.findAllByText('来访者正在回应')).not.toHaveLength(0)
    expect(screen.queryByText(/Director|Actor|合成|缓存|返修|对方停了一下/)).not.toBeInTheDocument()
  })

  it('尚未接通时不会提前显示通话中', async () => {
    liveMock.current = defaultLive({ connection: 'connecting', phase: 'listening' })
    renderSession('hotline')

    expect(await screen.findAllByText('正在接通…')).toHaveLength(2)
    expect(screen.queryByText('通话中')).not.toBeInTheDocument()
  })

  it('技术暂停保留原文、标明计时暂停并提供重新连接', async () => {
    const retry = vi.fn()
    liveMock.current = defaultLive({
      phase: 'technical_paused', retry,
      technicalPause: { message: '来访者的信号不太稳定', canRetry: true },
    })
    const user = userEvent.setup()
    renderSession('hotline')

    expect(await screen.findByText('来访者的信号不太稳定')).toBeInTheDocument()
    expect(screen.getByText('喂，你好。')).toBeInTheDocument()
    expect(screen.getByText('计时已暂停')).toBeInTheDocument()
    expect(screen.queryByText('麦克风已连接')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新连接' }))
    expect(retry).toHaveBeenCalledOnce()
  })

  it('重连尚未被后端确认时保持等待状态', async () => {
    const retry = vi.fn()
    liveMock.current = defaultLive({
      phase: 'technical_paused',
      retry,
      retrying: true,
      technicalPause: { message: '正在重新接通…', canRetry: true },
    })
    renderSession('hotline')

    const retryButton = await screen.findByRole('button', { name: '正在重新接通…' })
    expect(retryButton).toBeDisabled()
    expect(retry).not.toHaveBeenCalled()
  })

  it('缺少 API Key 时只提供设置和结束入口', async () => {
    liveMock.current = defaultLive({
      phase: 'technical_paused',
      technicalPause: { message: '请先在设置页配置阿里云百炼 API Key', canRetry: false },
    })
    renderSession('hotline')

    expect(await screen.findByRole('link', { name: '前往设置' })).toHaveAttribute('href', '/configure')
    expect(screen.queryByRole('button', { name: '重新连接' })).not.toBeInTheDocument()
  })

  it('在线咨询发送文字并支持 Ctrl+Enter，不创建语音控件', async () => {
    const sendText = vi.fn(() => true)
    liveMock.current = defaultLive({ sendText })
    const user = userEvent.setup()
    renderSession('online')

    const input = await screen.findByRole('textbox', { name: '输入本轮内容' })
    await user.type(input, '你愿意从哪件事说起？')
    await user.keyboard('{Control>}{Enter}{/Control}')
    expect(sendText).toHaveBeenCalledWith('你愿意从哪件事说起？')
    expect(input).toHaveValue('你愿意从哪件事说起？')
    expect(screen.queryByRole('button', { name: '我说完了' })).not.toBeInTheDocument()
    expect(screen.queryByText('麦克风已连接')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '当前会谈状态' })).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: '在线咨询消息' })).toBeInTheDocument()
    expect(screen.queryByText('通话中')).not.toBeInTheDocument()
    expect(screen.queryByText(/挂断/)).not.toBeInTheDocument()
  })

  it('在线咨询把一个来访者话轮按换行还原成多个有序气泡', async () => {
    const multiLineTurn = {
      id: 'c-online', sequence: 1, speaker: 'client' as const,
      text: '第一句\n\n第二句\r\n第三句', client_turn_id: 'opening-online',
    }
    liveMock.current = defaultLive({ transcript: [multiLineTurn] })
    renderSession('online', 'assessment', [multiLineTurn])

    const messages = await screen.findByRole('region', { name: '在线咨询消息' })
    const restoredTurn = messages.querySelector('[data-turn-id="c-online"]')
    expect(restoredTurn).not.toBeNull()
    expect(restoredTurn).toHaveAttribute('data-client-turn-id', 'opening-online')
    expect(restoredTurn?.querySelectorAll('.online-message-bubble')).toHaveLength(3)
    expect(restoredTurn?.querySelectorAll('.online-message-bubble')[0]).toHaveTextContent('第一句')
    expect(restoredTurn?.querySelectorAll('.online-message-bubble')[1]).toHaveTextContent('第二句')
    expect(restoredTurn?.querySelectorAll('.online-message-bubble')[2]).toHaveTextContent('第三句')
    expect(messages.querySelectorAll('[data-turn-id="c-online"]')).toHaveLength(1)
  })

  it('在线咨询渐进显示当前来访者消息和正在输入状态', async () => {
    liveMock.current = defaultLive({
      transcript: [],
      visitorReveal: {
        turnId: null,
        visibleSegments: ['我刚才又看了一遍。'],
        isTyping: true,
      },
    })
    renderSession('online', 'assessment', [])

    expect(await screen.findByText('我刚才又看了一遍。')).toHaveClass('online-message-bubble')
    expect(screen.getByText('对方正在输入…')).toBeInTheDocument()
  })

  it('在线消息只在用户原本接近底部时跟随新增内容', async () => {
    const firstTurn = persisted[0]
    liveMock.current = defaultLive({ transcript: [firstTurn] })
    const view = renderSession('online', 'assessment', [firstTurn])
    await screen.findByRole('region', { name: '在线咨询消息' })
    const scroll = document.querySelector<HTMLElement>('#online-transcript-content')!
    Object.defineProperties(scroll, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 500, writable: true },
    })
    scroll.scrollTop = 400
    fireEvent.scroll(scroll)

    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 650 })
    const secondTurn = persisted[1]
    liveMock.current = defaultLive({ transcript: [firstTurn, secondTurn] })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(scroll.scrollTop).toBe(650))

    scroll.scrollTop = 120
    fireEvent.scroll(scroll)
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 800 })
    liveMock.current = defaultLive({
      transcript: [firstTurn, secondTurn],
      visitorReveal: { turnId: null, visibleSegments: ['新消息'], isTyping: true },
    })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByText('新消息')).toBeInTheDocument())
    expect(scroll.scrollTop).toBe(120)
  })

  it('在线文字发送后保留草稿，服务端确认成功才清空', async () => {
    const sendText = vi.fn(() => true)
    liveMock.current = defaultLive({ transcript: [], sendText, textTurnStatus: 'idle' })
    const user = userEvent.setup()
    const view = renderSession('online', 'assessment', [])

    const input = await screen.findByRole('textbox', { name: '输入本轮内容' })
    await user.type(input, '我先陪你把今晚最担心的事情理清楚。')
    await user.click(screen.getByRole('button', { name: '发送' }))
    expect(sendText).toHaveBeenCalledOnce()
    expect(input).toHaveValue('我先陪你把今晚最担心的事情理清楚。')

    liveMock.current = defaultLive({
      transcript: [], sendText, textTurnStatus: 'committed',
    })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('textbox', { name: '输入本轮内容' })).toHaveValue(''))
  })

  it('在线文字发送失败后保留内容并允许再次发送', async () => {
    const sendText = vi.fn(() => true)
    liveMock.current = defaultLive({
      transcript: [], sendText, textTurnStatus: 'failed',
      inputError: '这条消息没有送达，请再试一次。',
    })
    const user = userEvent.setup()
    renderSession('online', 'assessment', [])

    const input = await screen.findByRole('textbox', { name: '输入本轮内容' })
    await user.type(input, '我还在，你可以慢一点说。')
    expect(screen.getByRole('alert')).toHaveTextContent('这条消息没有送达，请再试一次。')
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: '发送' }))
    expect(input).toHaveValue('我还在，你可以慢一点说。')
    expect(sendText).toHaveBeenCalledOnce()
  })

  it('在线咨询处理上一轮时禁用文字输入，避免覆盖尚未完成的话轮', async () => {
    const sendText = vi.fn(() => true)
    liveMock.current = defaultLive({ phase: 'acting', sendText })
    const user = userEvent.setup()
    renderSession('online')

    const input = await screen.findByRole('textbox', { name: '输入本轮内容' })
    const sendButton = screen.getByRole('button', { name: '发送' })
    expect(input).toBeDisabled()
    expect(sendButton).toBeDisabled()
    await user.keyboard('{Control>}{Enter}{/Control}')
    expect(sendText).not.toHaveBeenCalled()
  })

  it('结束正式测评时先确认，收到结束事件后留在终态页等待主动进入工作记录', async () => {
    const endSession = vi.fn()
    liveMock.current = defaultLive({ endSession })
    const user = userEvent.setup()
    const view = renderSession('online')
    await screen.findByRole('button', { name: '收起会谈原文' })

    await user.click(screen.getByRole('button', { name: '结束本次咨询' }))
    expect(screen.getByRole('dialog', { name: '确认结束在线咨询' })).toBeInTheDocument()
    expect(screen.getByText(/不会再等待来访者回应/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '确认结束咨询' }))
    expect(endSession).toHaveBeenCalledOnce()

    liveMock.current = defaultLive({ endedReason: 'user_ended', phase: 'ended' })
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes>
            <Route path="/session/:sessionId" element={<SessionPage />} />
            <Route path="/sessions/:sessionId/work-record" element={<div>工作记录页面</div>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: '你已结束本次咨询' })).toBeInTheDocument()
    expect(screen.queryByText('工作记录页面')).not.toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: '填写工作记录' }))
    expect(await screen.findByText('工作记录页面')).toBeInTheDocument()
  })

  it('正式会谈不显示软时间限制或收束提醒', async () => {
    renderSessionWithSoftDuration(1)

    await screen.findByRole('button', { name: '收起会谈原文' })
    expect(screen.queryByText(/建议时长|软时间|自然收束/)).not.toBeInTheDocument()
    expect(liveMock.current.endSession).not.toHaveBeenCalled()
  })

  it.each([
    ['natural_closure', '本次通话已自然结束'],
    ['technical_interruption', '通话因信号中断结束'],
  ])('结束原因 %s 使用独立终态文案', async (endedReason, heading) => {
    liveMock.current = defaultLive({ endedReason, phase: 'ended', connection: 'closed' })
    renderSession('hotline')

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '填写工作记录' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '立即挂断并结束' })).not.toBeInTheDocument()
  })

  it('刷新已经结束的会话时直接恢复终态，不重新进入实时通话', async () => {
    renderPersistedEndedSession('natural_closure')

    expect(await screen.findByRole('heading', { name: '本次通话已自然结束' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '填写工作记录' })).toBeInTheDocument()
    expect(screen.queryByText('麦克风已连接')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '我说完了' })).not.toBeInTheDocument()
  })

  it('刷新已经结束的在线咨询时使用咨询终态称谓', async () => {
    renderPersistedEndedSession('natural_closure', 'online')

    const endedPanel = await screen.findByRole('status', { name: '咨询结束' })
    expect(endedPanel).toHaveTextContent('咨询结束')
    expect(screen.queryByRole('status', { name: '通话结束' })).not.toBeInTheDocument()
  })

  it('收到结束事件后立即把会话终态写入共享查询缓存', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    })
    const restTurns = persisted.map((turn) => ({
      ...turn, provider: null, degraded: false, created_at: now, audio_available: false,
    }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
      session: session('hotline'), transcript: restTurns,
    })))
    const renderTree = () => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/session/s1']}>
          <Routes><Route path="/session/:sessionId" element={<SessionPage />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    const view = render(renderTree())
    await screen.findByRole('button', { name: '收起会谈原文' })

    liveMock.current = defaultLive({
      endedReason: 'natural_closure', phase: 'ended', connection: 'closed',
    })
    view.rerender(renderTree())
    expect(await screen.findByRole('heading', { name: '本次通话已自然结束' })).toBeInTheDocument()

    await waitFor(() => {
      const cached = queryClient.getQueryData<{ session: { status: string; end_reason: string | null } }>(['session', 's1'])
      expect(cached?.session.status).toBe('ended')
      expect(cached?.session.end_reason).toBe('natural_closure')
    })
  })
})
