import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { WorkRecordPage } from './WorkRecordPage'

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  putWorkRecord: vi.fn(),
  createReport: vi.fn(),
}))
vi.mock('../api/client', () => api)

const transcript = [
  {
    id: 'opening-client',
    sequence: 1,
    speaker: 'client',
    text: '喂，你好……有人吗？',
    client_turn_id: 'opening-1',
  },
  {
    id: 'worker-1',
    sequence: 2,
    speaker: 'worker',
    text: '你好，我在。你现在身边有人吗？',
    client_turn_id: 'voice-1',
  },
  {
    id: 'client-1',
    sequence: 3,
    speaker: 'client',
    text: '没有，就我自己。',
    client_turn_id: 'voice-1',
  },
  {
    id: 'worker-2',
    sequence: 4,
    speaker: 'worker',
    text: '这几天有没有想过伤害自己？',
    client_turn_id: 'voice-2',
  },
  {
    id: 'client-2',
    sequence: 5,
    speaker: 'client',
    text: '有过……晚上会冒出来。',
    client_turn_id: 'voice-2',
  },
  {
    id: 'worker-without-response',
    sequence: 6,
    speaker: 'worker',
    text: '你还在吗？',
    client_turn_id: 'voice-3',
  },
]

const savedDraft = {
  problem_understanding: '来访者独自在家，近期遭遇持续压力。',
  risk_level: 'high',
  risk_reasoning: '来访者确认近期出现自伤想法，且当前独处。',
  risk_evidence_turn_ids: ['worker-2', 'client-2'],
  missing_information: [],
  planned_actions: [],
  referral_decision: 'not_needed',
  supervision_decision: false,
  follow_up: '保持通话并联系可到场的现实支持。',
  limitations: '尚未确认危险物品可及性。',
}

function workRecordResponse(overrides = {}) {
  return {
    ...savedDraft,
    id: 'record-1',
    session_id: 'session-1',
    created_at: '2026-08-30T00:00:00Z',
    updated_at: '2026-08-30T00:00:00Z',
    ...overrides,
  }
}

function Location() {
  const location = useLocation()
  const navigate = useNavigate()
  return <><output>{location.pathname}</output><button type="button" onClick={() => navigate(-1)}>返回上一页</button></>
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      })}
    >
      <MemoryRouter
        initialEntries={['/session/session-1', '/sessions/session-1/work-record']}
        initialIndex={1}
      >
        <Routes>
          <Route path="/session/:sessionId" element={<Location />} />
          <Route path="/sessions/:sessionId/work-record" element={<WorkRecordPage />} />
          <Route path="/sessions/:sessionId/complete" element={<Location />} />
          <Route path="/report-jobs/:jobId" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillRequiredFields() {
  await userEvent.type(
    screen.getByLabelText('本次求助、当前需要与已确认信息'),
    '来访者独自在家，近期遭遇持续压力。',
  )
  await userEvent.click(screen.getByRole('radio', { name: '高风险' }))
  await userEvent.type(
    screen.getByLabelText('当前安全研判及依据'),
    '来访者确认近期出现自伤想法，且当前独处。',
  )
  await userEvent.type(
    screen.getByLabelText('行动状态与后续衔接'),
    '保持通话并联系可到场的现实支持。',
  )
  await userEvent.type(screen.getByLabelText('信息与判断限制'), '尚未确认危险物品可及性。')
}

function expectFormLocked() {
  const controls = document.querySelectorAll('input, textarea, select')
  expect(controls.length).toBeGreaterThan(0)
  controls.forEach((control) => expect(control).toBeDisabled())
}

describe('热线工作记录页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.getSession.mockResolvedValue({
      session: { id: 'session-1', scene: 'hotline', media: 'voice' },
      transcript,
    })
  })

  it('主动开场可单独选择，其余问答保持成组且工作者单边话轮隐藏', async () => {
    renderPage()

    expect(await screen.findByText('喂，你好……有人吗？')).toBeInTheDocument()
    expect(await screen.findByText('你好，我在。你现在身边有人吗？')).toBeInTheDocument()
    expect(screen.getByText('没有，就我自己。')).toBeInTheDocument()
    expect(screen.getByText('这几天有没有想过伤害自己？')).toBeInTheDocument()
    expect(screen.getByText('有过……晚上会冒出来。')).toBeInTheDocument()
    expect(screen.queryByText('你还在吗？')).not.toBeInTheDocument()
    expect(screen.queryByText('本次未形成有效回应。')).not.toBeInTheDocument()
    expect(screen.queryByText('voice-1')).not.toBeInTheDocument()
    expect(screen.queryByText('client-1')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('证据回合')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', {
      name: '关键判断与处置的原话依据',
    })).toBeInTheDocument()
    const openingEvidence = screen.getByRole('checkbox', {
      name: '纳入关键判断与处置依据：原话片段 1',
    })
    expect(openingEvidence).toBeEnabled()
    await userEvent.click(openingEvidence)
    expect(openingEvidence).toBeChecked()
    expect(screen.getByText('已选 1 个证据片段')).toBeInTheDocument()
  })

  it('热线记录只提示填写本次实际听见的语音线索', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: '热线工作记录' })).toBeInTheDocument()
    expect(screen.getByText(/只记录本次实际听见的语速、停顿或声音变化/)).toBeInTheDocument()
    expect(screen.getByText(/不要把系统生成的声音提示当成事实/)).toBeInTheDocument()
  })

  it('在线记录只显示文字场域文案，不残留热线专属词', async () => {
    api.getSession.mockResolvedValue({
      session: { id: 'session-1', scene: 'online', media: 'text' },
      transcript,
    })
    const { container } = renderPage()

    expect(await screen.findByRole('heading', { name: '在线咨询工作记录' })).toBeInTheDocument()
    expect(screen.getByText(/文字回复节奏、连续短消息或明显停顿/)).toBeInTheDocument()
    expect(screen.getByText('本次求助、当前需要与已确认信息')).toBeInTheDocument()
    expect(screen.getByText('行动状态与后续衔接')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/HOTLINE|热线|来电|通话|接线员|声音/)
  })

  it('场域尚未加载时使用中性工作记录文案', () => {
    api.getSession.mockReturnValue(new Promise(() => undefined))
    const { container } = renderPage()

    expect(screen.getByRole('heading', { name: '专业工作记录' })).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/HOTLINE|热线|来电|通话|接线员|声音/)
  })

  it('工作类别不表示已经执行，行动状态分别记录完成、同意与计划', async () => {
    renderPage()

    expect(await screen.findByText('本次涉及的工作类别')).toBeInTheDocument()
    expect(screen.getByText(/勾选本次已经讨论或采取过的工作类别/)).toBeInTheDocument()
    expect(screen.getByText(/不等同于相应行动已经执行/)).toBeInTheDocument()
    expect(screen.getByText(/已经完成、已经同意、准备之后做/)).toBeInTheDocument()
    expect(screen.getByLabelText('需要负责人或督导进一步讨论')).toBeInTheDocument()
    expect(screen.queryByText(/提交值班负责人|提交督导/)).not.toBeInTheDocument()
  })

  it('保存工作记录后立即创建报告任务，两步成功才清草稿并进入任务页', async () => {
    api.putWorkRecord.mockResolvedValue(workRecordResponse())
    api.createReport.mockResolvedValue({ id: 'job-1', stage: 'queued' })
    renderPage()
    await screen.findByText('没有，就我自己。')
    await fillRequiredFields()

    await userEvent.click(
      screen.getByRole('checkbox', { name: '纳入关键判断与处置依据：原话片段 3' }),
    )
    expect(screen.getByText('已选 1 个证据片段')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '提交工作记录' }))

    await waitFor(() => expect(api.putWorkRecord).toHaveBeenCalledTimes(1))
    expect(api.putWorkRecord).toHaveBeenCalledWith(
      'session-1',
      expect.objectContaining({ risk_evidence_turn_ids: ['worker-2', 'client-2'] }),
    )
    await waitFor(() => expect(api.createReport).toHaveBeenCalledWith('session-1'))
    expect(await screen.findByText('/report-jobs/job-1')).toBeInTheDocument()
    expect(localStorage.getItem('work-record-draft:session-1')).toBeNull()
  })

  it('PUT 请求未完成时锁定全部编辑控件，阻止请求期间的修改', async () => {
    let resolvePut!: (value: ReturnType<typeof workRecordResponse>) => void
    api.putWorkRecord.mockReturnValue(new Promise((resolve) => { resolvePut = resolve }))
    api.createReport.mockResolvedValue({ id: 'job-after-pending', stage: 'queued' })
    renderPage()
    await screen.findByText('没有，就我自己。')
    await fillRequiredFields()

    const understanding = screen.getByLabelText('本次求助、当前需要与已确认信息')
    const valueBeforeSubmit = understanding.getAttribute('value') ?? savedDraft.problem_understanding
    await userEvent.click(screen.getByRole('button', { name: '提交工作记录' }))

    expect(await screen.findByRole('button', { name: '正在保存…' })).toBeDisabled()
    expectFormLocked()
    await userEvent.type(understanding, '请求期间不应接受这段修改')
    expect(understanding).toHaveValue(valueBeforeSubmit)

    resolvePut(workRecordResponse())
    expect(await screen.findByText('/report-jobs/job-after-pending')).toBeInTheDocument()
  })

  it('提交失败时保留已填写内容和已选原话证据', async () => {
    api.putWorkRecord.mockRejectedValue(new Error('线路记录暂时未能保存'))
    renderPage()
    await screen.findByText('没有，就我自己。')
    await fillRequiredFields()
    await userEvent.click(
      screen.getByRole('checkbox', { name: '纳入关键判断与处置依据：原话片段 2' }),
    )

    await userEvent.click(screen.getByRole('button', { name: '提交工作记录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('已填写内容仍保留')
    const draft = JSON.parse(localStorage.getItem('work-record-draft:session-1') ?? '{}')
    expect(draft.problem_understanding).toContain('来访者独自在家')
    expect(draft.risk_evidence_turn_ids).toEqual(['worker-1', 'client-1'])
    expect(
      screen.getByRole('checkbox', { name: '纳入关键判断与处置依据：原话片段 2' }),
    ).toBeChecked()
    expect(localStorage.getItem('work-record-saved:session-1')).toBeNull()
  })

  it('报告任务响应丢失后锁定已保存快照，同页重试只调用幂等创建接口', async () => {
    api.putWorkRecord.mockResolvedValue(workRecordResponse({
      problem_understanding: '来访者独处，近期持续承受压力。',
      risk_evidence_turn_ids: ['worker-2', 'client-2'],
      missing_information: ['危险物品可及性'],
    }))
    api.createReport
      .mockRejectedValueOnce(new Error('ReportProvider traceback: secret-internal-detail'))
      .mockResolvedValueOnce({ id: 'job-retry', stage: 'queued' })
    renderPage()
    await screen.findByText('没有，就我自己。')
    await fillRequiredFields()

    await userEvent.click(screen.getByRole('button', { name: '提交工作记录' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('报告分析任务暂时未能创建')
    expect(screen.queryByText(/traceback|secret-internal-detail/i)).not.toBeInTheDocument()
    expect(localStorage.getItem('work-record-draft:session-1')).not.toBeNull()
    expect(screen.getByLabelText('本次求助、当前需要与已确认信息')).toHaveValue(
      '来访者独处，近期持续承受压力。',
    )
    const savedMarker = localStorage.getItem('work-record-saved:session-1') ?? ''
    expect(savedMarker).toContain('"problem_understanding"')
    expect(JSON.parse(savedMarker)).toEqual(
      expect.objectContaining({
        problem_understanding: '来访者独处，近期持续承受压力。',
        risk_level: 'high',
        risk_evidence_turn_ids: ['worker-2', 'client-2'],
        missing_information: ['危险物品可及性'],
      }),
    )
    expect(JSON.parse(savedMarker)).not.toHaveProperty('id')
    expect(screen.getByText('工作记录已经保存，报告将使用这个版本。请重试生成报告。')).toBeInTheDocument()
    expectFormLocked()
    expect(screen.getByRole('button', { name: '重试生成报告' })).toBeEnabled()

    const draftBeforeAttemptedEdit = localStorage.getItem('work-record-draft:session-1')
    await userEvent.type(screen.getByLabelText('本次求助、当前需要与已确认信息'), '这段修改不应写入草稿')
    expect(screen.getByLabelText('本次求助、当前需要与已确认信息')).toHaveValue(
      '来访者独处，近期持续承受压力。',
    )
    expect(localStorage.getItem('work-record-draft:session-1')).toBe(draftBeforeAttemptedEdit)

    await userEvent.click(screen.getByRole('button', { name: '重试生成报告' }))

    expect(await screen.findByText('/report-jobs/job-retry')).toBeInTheDocument()
    expect(api.putWorkRecord).toHaveBeenCalledTimes(1)
    expect(api.createReport).toHaveBeenCalledTimes(2)
    expect(localStorage.getItem('work-record-draft:session-1')).toBeNull()
    expect(localStorage.getItem('work-record-saved:session-1')).toBeNull()
  })

  it('刷新后优先展示已保存快照并保持锁定，不覆写未保存草稿', async () => {
    const unsavedDraft = { ...savedDraft, problem_understanding: '这是没有保存到服务端的后续修改。' }
    const draftRaw = JSON.stringify(unsavedDraft)
    localStorage.setItem('work-record-draft:session-1', draftRaw)
    localStorage.setItem('work-record-saved:session-1', JSON.stringify(savedDraft))
    api.createReport.mockResolvedValue({ id: 'job-restored', stage: 'queued' })
    renderPage()

    expect(await screen.findByLabelText('本次求助、当前需要与已确认信息')).toHaveValue(savedDraft.problem_understanding)
    expect(screen.getByText('工作记录已经保存，报告将使用这个版本。请重试生成报告。')).toBeInTheDocument()
    expectFormLocked()
    expect(localStorage.getItem('work-record-draft:session-1')).toBe(draftRaw)

    await userEvent.click(screen.getByRole('button', { name: '重试生成报告' }))

    expect(await screen.findByText('/report-jobs/job-restored')).toBeInTheDocument()
    expect(api.putWorkRecord).not.toHaveBeenCalled()
    expect(api.createReport).toHaveBeenCalledOnce()
    expect(localStorage.getItem('work-record-draft:session-1')).toBeNull()
    expect(localStorage.getItem('work-record-saved:session-1')).toBeNull()
  })

  it('逐字稿读取失败时仍可填写记录，并给出原话证据提示', async () => {
    api.getSession.mockRejectedValue(new Error('无法读取逐字稿'))
    renderPage()

    expect(await screen.findByText('原话记录暂时无法读取')).toBeInTheDocument()
    expect(screen.getByLabelText('本次求助、当前需要与已确认信息')).toBeEnabled()
  })
})
