import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { EvidenceRef, WorkRecord } from '../api/contracts'
import { EvidenceDrawer } from './EvidenceDrawer'

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  getWorkRecord: vi.fn(),
}))
vi.mock('../api/client', () => api)

const workRecord: WorkRecord = {
  id: 'record-1',
  session_id: 'session-1',
  problem_understanding: '来访者近期失眠，仍愿意求助。',
  risk_level: 'high',
  risk_reasoning: '来访者提到轻生念头，尚未确认危险物品可及性，但仍能联系室友。',
  risk_evidence_turn_ids: ['turn-2', 'turn-4'],
  missing_information: ['危险物品可及性', '既往医疗记录'],
  planned_actions: ['stay_connected', 'contact_support'],
  referral_decision: 'urgent',
  supervision_decision: true,
  follow_up: '保持在线直至室友到场。',
  limitations: '判断仅基于本次对话和来访者自述。',
  created_at: '2026-08-30T00:00:00Z',
  updated_at: '2026-08-30T00:00:00Z',
}

function renderDrawer(evidence: EvidenceRef, onClose = vi.fn()) {
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })}>
      <EvidenceDrawer sessionId="session-1" evidence={evidence} onClose={onClose} />
    </QueryClientProvider>,
  )
}

describe('原始材料抽屉', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getWorkRecord.mockResolvedValue(workRecord)
    api.getSession.mockResolvedValue({ session: {}, transcript: [] })
  })

  it('通过 portal 直接挂到页面根部，避免报告页祖先变换限制抽屉定位', () => {
    renderDrawer({ kind: 'audio_event', event_id: 'event-for-portal-check' })

    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    expect(drawer.parentElement).toBe(document.body)
  })

  it('使用统一名称读取冻结工作记录，展示完整原文并高亮报告引用', async () => {
    renderDrawer({
      kind: 'work_record',
      field: 'risk_reasoning',
      quote: '尚未确认危险物品可及性',
    })

    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    expect(within(drawer).getByRole('heading', { name: '查看原始材料' })).toBeInTheDocument()
    expect(within(drawer).getByRole('button', { name: '关闭原始材料' })).toBeInTheDocument()
    expect(within(drawer).getByText('原始材料')).toBeInTheDocument()
    const source = screen.getByRole('region', { name: '工作记录原文' })
    await within(source).findByText('尚未确认危险物品可及性', { selector: 'mark' })
    expect(api.getWorkRecord).toHaveBeenCalledWith('session-1')
    expect(source).toHaveTextContent(workRecord.risk_reasoning)
    expect(within(source).getByText('尚未确认危险物品可及性', { selector: 'mark' })).toBeInTheDocument()
    expect(source).toHaveTextContent('工作记录原文')
    expect(source).toHaveTextContent('以上内容取自生成报告时冻结保存的工作记录。')
    expect(drawer).not.toHaveTextContent(/字段|risk_reasoning|high/)
  })

  it('数组逐项显示，枚举和布尔值使用中文标签', async () => {
    const first = renderDrawer({
      kind: 'work_record',
      field: 'planned_actions',
      quote: 'contact_support',
    })

    let source = screen.getByRole('region', { name: '工作记录原文' })
    expect(await within(source).findAllByRole('listitem')).toHaveLength(2)
    expect(source).toHaveTextContent('保持连接与陪伴')
    expect(within(source).getByText('联系现实支持', { selector: 'mark' })).toBeInTheDocument()
    expect(source).not.toHaveTextContent(/contact_support|stay_connected|planned_actions|字段/)

    first.unmount()
    renderDrawer({ kind: 'work_record', field: 'supervision_decision', quote: '是' })

    source = screen.getByRole('region', { name: '工作记录原文' })
    expect(await within(source).findByText('是', { selector: 'mark' })).toBeInTheDocument()
  })

  it('风险依据中的两个内部通话编号解析为自然序号、角色和逐字原话', async () => {
    api.getWorkRecord.mockResolvedValue({
      ...workRecord,
      risk_evidence_turn_ids: ['turn-internal-1', 'turn-internal-2'],
    })
    api.getSession.mockResolvedValue({
      session: {},
      transcript: [
        { id: 'turn-internal-1', sequence: 1, speaker: 'client', text: '我昨晚几乎没睡，一闭眼就乱想。' },
        { id: 'turn-internal-2', sequence: 2, speaker: 'worker', text: '你刚才说会乱想，里面有没有伤害自己的念头？' },
      ],
    })
    renderDrawer({
      kind: 'work_record',
      field: 'risk_evidence_turn_ids',
      quote: 'turn-internal-2',
    })

    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    const source = screen.getByRole('region', { name: '工作记录原文' })
    const items = await within(source).findAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('引用的通话原文 1')
    expect(items[0]).toHaveTextContent('来电者')
    expect(items[0]).toHaveTextContent('我昨晚几乎没睡，一闭眼就乱想。')
    expect(items[1]).toHaveTextContent('引用的通话原文 2')
    expect(items[1]).toHaveTextContent('接线人员')
    expect(items[1]).toHaveTextContent('你刚才说会乱想，里面有没有伤害自己的念头？')
    expect(api.getSession).toHaveBeenCalledWith('session-1')
    const exposedContent = [
      drawer.textContent,
      ...Array.from(drawer.querySelectorAll('[aria-label]'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(exposedContent).not.toMatch(/turn-internal-[12]|risk_evidence_turn_ids/)
  })

  it('风险依据中某个编号找不到时明确说明未找到对应原文且不回显编号', async () => {
    api.getWorkRecord.mockResolvedValue({
      ...workRecord,
      risk_evidence_turn_ids: ['turn-internal-1', 'turn-internal-2'],
    })
    api.getSession.mockResolvedValue({
      session: {},
      transcript: [
        { id: 'turn-internal-1', sequence: 1, speaker: 'client', text: '我最近总睡不好。' },
      ],
    })
    renderDrawer({
      kind: 'work_record',
      field: 'risk_evidence_turn_ids',
      quote: 'turn-internal-2',
    })

    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    const source = screen.getByRole('region', { name: '工作记录原文' })
    const items = await within(source).findAllByRole('listitem')
    expect(items[0]).toHaveTextContent('引用的通话原文 1')
    expect(items[0]).toHaveTextContent('来电者')
    expect(items[0]).toHaveTextContent('我最近总睡不好。')
    expect(items[1]).toHaveTextContent('引用的通话原文 2')
    expect(items[1]).toHaveTextContent('未在本次通话记录中找到对应原文')
    const exposedContent = [
      drawer.textContent,
      ...Array.from(drawer.querySelectorAll('[aria-label]'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(exposedContent).not.toMatch(/turn-internal-[12]|risk_evidence_turn_ids/)
  })

  it('风险依据的通话记录读取失败时显示自然核对说明且不回显编号', async () => {
    api.getSession.mockRejectedValue(new Error('读取失败'))
    renderDrawer({
      kind: 'work_record',
      field: 'risk_evidence_turn_ids',
      quote: 'turn-internal-failed',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('本次通话记录暂时无法读取，工作记录中的风险判断原话暂时无法核对。')
    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    const exposedContent = [
      drawer.textContent,
      ...Array.from(drawer.querySelectorAll('[aria-label]'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(exposedContent).not.toMatch(/turn-internal-failed|risk_evidence_turn_ids/)
  })

  it('工作记录列表为空时使用自然中文说明', async () => {
    api.getWorkRecord.mockResolvedValue({ ...workRecord, planned_actions: [] })
    renderDrawer({
      kind: 'work_record',
      field: 'planned_actions',
      quote: '报告里保存的后续行动',
    })

    const source = screen.getByRole('region', { name: '工作记录原文' })
    expect(await within(source).findByText('这部分没有记录具体内容。')).toBeInTheDocument()
    expect(source).not.toHaveTextContent('字段')
  })

  it('读取失败时展示报告中保存的引用', async () => {
    api.getWorkRecord.mockRejectedValue(new Error('读取失败'))
    renderDrawer({
      kind: 'work_record',
      field: 'limitations',
      quote: '报告中保存的限制说明',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('工作记录暂时无法读取，报告保存的原话如下。')
    expect(within(alert).getByText('报告中保存的限制说明')).toBeInTheDocument()
  })

  it('完整字段中找不到引用时同时保留完整值和报告引用', async () => {
    renderDrawer({
      kind: 'work_record',
      field: 'limitations',
      quote: '报告里仍需保留的原话',
    })

    const source = screen.getByRole('region', { name: '工作记录原文' })
    await within(source).findByText('报告里仍需保留的原话')
    expect(source).toHaveTextContent(workRecord.limitations)
    expect(source).toHaveTextContent('报告里仍需保留的原话')
    expect(source).toHaveTextContent('当前工作记录中未找到这段引用，报告保存的原话如下。')
    expect(source).toHaveTextContent('以上内容取自生成报告时冻结保存的工作记录。')
    expect(source).not.toHaveTextContent('字段')
  })

  it('对话材料显示相邻语境和自然角色名，并保留 Escape 与焦点恢复', async () => {
    const user = userEvent.setup()
    const trigger = document.createElement('button')
    document.body.append(trigger)
    trigger.focus()
    const onClose = vi.fn()
    api.getSession.mockResolvedValue({
      session: {},
      transcript: [
        { id: 'turn-internal-1', sequence: 1, speaker: 'client', text: '我很难受。' },
        { id: 'turn-internal-2', sequence: 2, speaker: 'worker', text: '我在听。' },
      ],
    })
    const { unmount } = renderDrawer({
      kind: 'dialogue',
      turn_id: 'turn-internal-2',
      quote: '我在听。',
    }, onClose)

    expect(await screen.findByText('我在听。')).toBeInTheDocument()
    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('通话转写 · 引用内容及前后语境')
    expect(drawer).toHaveTextContent('来电者')
    expect(drawer).toHaveTextContent('接线人员')
    const exposedContent = [
      drawer.textContent,
      ...Array.from(drawer.querySelectorAll('[aria-label]'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(exposedContent).not.toMatch(/turn-internal|目标话轮|受测者|来访者|\bworker\b|\bclient\b/)
    expect(api.getSession).toHaveBeenCalledWith('session-1')
    expect(api.getWorkRecord).not.toHaveBeenCalled()
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    expect(trigger).toHaveFocus()
    trigger.remove()
  })

  it('通话记录读取失败或找不到引用时保留报告原话且不暴露内部定位', async () => {
    api.getSession.mockRejectedValue(new Error('读取失败'))
    const failed = renderDrawer({
      kind: 'dialogue',
      turn_id: 'turn-secret-failed',
      quote: '报告保存的通话原话',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('通话记录暂时无法读取，报告保存的原话如下。')
    expect(alert).toHaveTextContent('报告保存的通话原话')
    expect(screen.getByRole('dialog', { name: '查看原始材料' })).not.toHaveTextContent(/turn-secret-failed|目标话轮/)

    failed.unmount()
    api.getSession.mockResolvedValue({ session: {}, transcript: [] })
    renderDrawer({
      kind: 'dialogue',
      turn_id: 'turn-secret-missing',
      quote: '仍需保留的通话原话',
    })

    expect(await screen.findByText('当前通话记录中没有找到这段引用，报告保存的原话如下。')).toBeInTheDocument()
    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('仍需保留的通话原话')
    expect(drawer).not.toHaveTextContent(/turn-secret-missing|目标话轮/)
  })

  it('声音材料明确说明本次未分析的范围且不显示事件编号', () => {
    renderDrawer({ kind: 'audio_event', event_id: 'event-internal-audio-7' })

    const drawer = screen.getByRole('dialog', { name: '查看原始材料' })
    const source = screen.getByRole('region', { name: '声音材料说明' })
    expect(source).toHaveTextContent('声音材料说明')
    expect(within(source).getByRole('heading', { name: '本次未分析声音表现' })).toBeInTheDocument()
    expect(source).toHaveTextContent('本次判断只依据通话转写和工作记录，未分析语速、停顿、语调等声音表现。')
    const exposedContent = [
      drawer.textContent,
      ...Array.from(drawer.querySelectorAll('[aria-label]'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(exposedContent).not.toMatch(/event-internal-audio-7|audio_event/)
  })
})
