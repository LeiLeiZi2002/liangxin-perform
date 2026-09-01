import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { EvidenceRef } from '../api/contracts'
import { targetDescriptions } from '../api/labels'
import { ReportPage } from './ReportPage'

const api = vi.hoisted(() => ({
  getReport: vi.fn(), getSession: vi.fn(), getWorkRecord: vi.fn(), retryReportJob: vi.fn(),
}))
vi.mock('../api/client', () => api)

const dialogueEvidence = {
  unit_id: 'unit-1', target: 'C1', indicator_id: 'C1.1', direction: 'support', strength: 'strong',
  context: '先承接情绪，再确认安全。', alternative_reading: null,
  ref: { kind: 'dialogue', turn_id: 'turn-2', quote: '听起来这几天非常难熬，我想先确认你现在是否安全。' },
}
const secondDialogueEvidence = {
  unit_id: 'unit-3', target: 'C1', indicator_id: 'C1.2', direction: 'support', strength: 'strong',
  context: '邀请来电者共同选择现实支持。', alternative_reading: null,
  ref: { kind: 'dialogue', turn_id: 'turn-4', quote: '我们一起看看现在能联系谁。' },
}
const thirdDialogueEvidence = {
  unit_id: 'unit-4', target: 'C1', indicator_id: 'C1.3', direction: 'support', strength: 'moderate',
  context: '结束前再次确认下一步安排。', alternative_reading: null,
  ref: { kind: 'dialogue', turn_id: 'turn-5', quote: '在挂断前，我们再确认一下接下来怎么做。' },
}
const counterEvidence = {
  unit_id: 'unit-2', target: 'C1', indicator_id: 'C1.2', direction: 'limit', strength: 'moderate',
  context: '提问衔接较快。', alternative_reading: '可能受到通话时间限制。',
  ref: { kind: 'work_record', field: 'limitations', quote: '尚未确认危险物品可及性。' },
}

function scoredDimension(target: string, name: string, evidence = [dialogueEvidence]) {
  const indicatorId = target === 'C1' ? 'C1.respect' : 'S1a.screening_scope'
  const supportingEvidence = target === 'C1'
    ? [dialogueEvidence, secondDialogueEvidence, thirdDialogueEvidence]
    : evidence
  return {
    target, name,
    level_anchor: '能稳定展现目标行为，并能根据复杂互动调整做法。',
    result: {
      target, level: 3, unscored_reason: null, analysis_outcome: 'ok',
      opportunities: [
        {
          declared_target: target, kind: 'required', fulfilled: true,
          indicator_ids: [indicatorId], complex_opportunity: target === 'C1',
        },
        ...(target === 'C1' ? [{
          declared_target: target, kind: 'conditional', fulfilled: false,
          indicator_ids: ['C1.repair'], complex_opportunity: false,
        }] : []),
      ],
      indicator_states: { [`${target}.1`]: 'demonstrated' },
      pattern: '先承接当事人的处境，再进入必要核对。',
      rationale: '代表性原话与工作记录共同支持这一判断。',
      evidence: supportingEvidence, counter_evidence: target === 'C1' ? [counterEvidence] : [],
      representative_unit_ids: supportingEvidence.map((item) => item.unit_id), limiting_unit_ids: target === 'C1' ? ['unit-2'] : [],
      conditional_unavailable: target === 'C1' ? ['关系破裂后的修复'] : [],
      caps_applied: target === 'C1' ? ['no_complex_opportunity'] : [],
      evidence_confidence: 'high', evidence_confidence_factors: ['原话引用可定位', '两类材料相互支持'],
      next_level_gap: ['说明关键提问的目的，并邀请当事人补充。'],
    },
  }
}

const report = {
  id: 'report-1', session_id: 'session-1', job_id: 'job-1', case_id: 'crisis_student_main',
  scene: 'hotline', media: 'voice',
  summary: {
    scored_core_count: 1,
    unscored: [['C2', 'no_opportunity']],
    analysis_failed: ['C3'],
    activated_modules: ['S1a'],
    inactive_modules: [['S1b', '本个案未声明完整风险评估机会'], ['S4', '本次材料未触发该模块']],
    bottom_line_events: [], screening_gap: true,
    level_distribution: '九个核心维度中一个形成等级，一个因无观察机会未评分，一个分析未完成。',
    next_behaviors: ['说明关键提问的目的，并邀请当事人补充。'],
  },
  dimensions: [
    scoredDimension('C1', '尊重、真诚与非评判性沟通'),
    {
      target: 'C2', name: '倾听、情绪识别与回应', level_anchor: null,
      result: {
        target: 'C2', level: null, unscored_reason: 'no_opportunity', analysis_outcome: 'ok',
        opportunities: [{
          declared_target: 'C2', kind: 'required', fulfilled: false,
          indicator_ids: ['C2.content_tracking'], complex_opportunity: false,
        }],
        indicator_states: { 'C2.1': 'no_opportunity' }, pattern: '', rationale: '案例未提供对应观察机会。',
        evidence: [], counter_evidence: [], representative_unit_ids: [], limiting_unit_ids: [],
        conditional_unavailable: [], caps_applied: [], evidence_confidence: null,
        evidence_confidence_factors: [], next_level_gap: [],
      },
    },
    {
      target: 'C3', name: '关切澄清与信息收集', level_anchor: null,
      result: {
        target: 'C3', level: null, unscored_reason: null, analysis_outcome: 'analysis_failed',
        opportunities: [{
          declared_target: 'C3', kind: 'required', fulfilled: true,
          indicator_ids: ['C3.call_reason'], complex_opportunity: false,
        }],
        indicator_states: {}, pattern: '', rationale: '', evidence: [], counter_evidence: [],
        representative_unit_ids: [], limiting_unit_ids: [], conditional_unavailable: [], caps_applied: [],
        evidence_confidence: null, evidence_confidence_factors: [], next_level_gap: [],
      },
    },
    scoredDimension('S1a', '基础风险筛查'),
  ],
  bottom_line_events: [{
    id: 'event-1', category: 'false_confidentiality', detection: 'semantic',
    refs: [
      { kind: 'work_record', field: 'limitations', quote: '已承诺所有内容绝不外传。' },
      { kind: 'audio_event', event_id: 'audio-1' },
    ],
    description: '保密边界表述需要核对。', reasoning: '材料中出现绝对保密表述。', repair_observed: false,
  }],
  material_conflicts: [{
    id: 'conflict-1',
    dialogue_ref: { kind: 'dialogue', turn_id: 'turn-3', quote: '我还没有联系任何人。' },
    work_record_ref: { kind: 'work_record', field: 'follow_up', quote: '已联系其室友到场。' },
    description: '是否已联系现实支持者的记载不一致。', affected_targets: ['C1', 'C9'],
    impact: '限制 C1 与 C9 的判断。',
  }],
  screening_gap: true,
  disclaimers: ['不得用于胜任判定。', '不得用于有后果的人事决定。', '不得跨案例比较。', '不得推断真实工作表现。'],
  rubric_fingerprint: 'rubric-v1', case_package_fingerprint: 'case-v1', model_fingerprint: 'model-v1',
  prompt_fingerprint: 'prompt-v1', input_fingerprint: 'input-v1', ai_draft_status: 'partial',
  created_at: '2026-08-30T00:00:00Z',
}

function Location() {
  return <output>{useLocation().pathname}</output>
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: {
      queries: { retry: false }, mutations: { retry: false },
    } })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/reports/report-1']}>
        <Routes>
          <Route path="/reports/:reportId" element={<ReportPage />} />
          <Route path="/report-jobs/:jobId" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...view, queryClient }
}

describe('证据报告页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getReport.mockResolvedValue(report)
    api.getSession.mockResolvedValue({ session: {}, transcript: [
      { id: 'turn-1', sequence: 1, speaker: 'client', text: '我最近实在撑不住了。' },
      { id: 'turn-2', sequence: 2, speaker: 'worker', text: '听起来这几天非常难熬，我想先确认你现在是否安全。' },
      { id: 'turn-3', sequence: 3, speaker: 'client', text: '我还没有联系任何人。' },
      { id: 'turn-4', sequence: 4, speaker: 'worker', text: '我们一起看看现在能联系谁。' },
      { id: 'turn-5', sequence: 5, speaker: 'worker', text: '在挂断前，我们再确认一下接下来怎么做。' },
    ] })
    api.getWorkRecord.mockResolvedValue({
      id: 'record-1', session_id: 'session-1',
      problem_understanding: '来访者近期承受较大压力。', risk_level: 'high',
      risk_reasoning: '来访者表达了绝望感。', risk_evidence_turn_ids: ['turn-2'],
      missing_information: ['危险物品可及性'], planned_actions: ['contact_support'],
      referral_decision: 'urgent', supervision_decision: true,
      follow_up: '已联系其室友到场。',
      limitations: '尚未确认危险物品可及性。已承诺所有内容绝不外传。',
      created_at: '2026-08-30T00:00:00Z', updated_at: '2026-08-30T00:00:00Z',
    })
  })

  it('使用中文报告标题、案例名与待核对身份，并隐藏所有接口内部标识', async () => {
    const { container } = renderPage()

    const title = await screen.findByRole('heading', { name: '初阶心理服务从业者胜任力测评报告' })
    expect(title.closest('article')).toBe(container.firstElementChild)
    expect(screen.getByText('管理者查看 · 待核对分析稿')).toBeInTheDocument()
    expect(screen.getByText('案例：明早她就到了')).toBeInTheDocument()
    expect(screen.getByText(/生成时间：/)).toBeInTheDocument()
    expect(screen.getByText('本报告由大模型依据冻结的会谈原文和工作记录生成，正式使用前必须逐项核对原始材料。')).toBeInTheDocument()
    expect(screen.getByText('仅用于发展性反馈。')).toBeInTheDocument()

    const userFacingContent = [
      container.textContent,
      ...Array.from(container.getElementsByTagName('*'))
        .filter((element) => element.hasAttribute('aria-label'))
        .map((element) => element.getAttribute('aria-label')),
    ].join(' ')
    expect(userFacingContent).not.toMatch(/crisis_student_main|audio-1|C1\.respect|C1\.repair|\b(?:C[1-9]|S1a|S1b|S[2-8])\b|\brequired\b|\bconditional\b/)
    expect(title).toBeInTheDocument()
    expect(screen.queryByText(/总分|百分制|覆盖率|复核推荐|一致性百分比/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/雷达图/)).not.toBeInTheDocument()
  })

  it('在线咨询完整报告统一使用文字媒介文案并保留合法事实与热线资源', async () => {
    const user = userEvent.setup()
    const onlineReport = structuredClone(report)
    onlineReport.scene = 'online'
    onlineReport.media = 'text'
    onlineReport.case_id = 'marriage_boundary_main'
    const onlineDimensions = [
      ['C1', '尊重性沟通与关系维护', 'C1.respect'],
      ['C2', '倾听与情绪理解', 'C2.content_tracking'],
      ['C3', '求助诉求澄清与工作焦点', 'C3.call_reason'],
      ['C4', '信息整合与专业判断', 'C4.evidence_boundary'],
      ['C5', '支持性介入与协同行动', 'C5.resources'],
      ['C6', '文字表达与互动过程管理', 'C6.clarity'],
      ['C7', '服务边界与伦理责任', 'C7.role_scope'],
      ['C8', '会谈结束与后续安排', 'C8.review'],
      ['C9', '工作记录的准确性与可追溯性', 'C9.source_distinction'],
      ['S1a', '基础风险筛查', 'S1a.screening_scope'],
    ] as const
    onlineReport.dimensions = onlineDimensions.map(([target, name, indicatorId]) => {
      const dimension = scoredDimension(target, name)
      dimension.result.opportunities = [{
        declared_target: target,
        kind: 'required',
        fulfilled: true,
        indicator_ids: [indicatorId],
        complex_opportunity: false,
      }]
      dimension.result.pattern = '本次表现能够结合来访者当前处境展开。'
      dimension.result.rationale = '现有会谈原文与工作记录能够支持这一判断。'
      dimension.result.evidence = []
      dimension.result.counter_evidence = []
      dimension.result.representative_unit_ids = []
      dimension.result.limiting_unit_ids = []
      dimension.result.conditional_unavailable = []
      dimension.result.caps_applied = []
      dimension.result.evidence_confidence_factors = ['现有材料可以定位核对']
      dimension.result.next_level_gap = ['还需要观察更复杂情境下的调整方式。']
      return dimension
    })
    const c6 = onlineReport.dimensions.find((dimension) => dimension.target === 'C6')!
    c6.result.rationale = '她看到丈夫手机亮起后，仍不能确认丈夫是否与对方通话；必要时可以拨打心理援助热线。'
    onlineReport.bottom_line_events = []
    onlineReport.material_conflicts = []
    onlineReport.screening_gap = false
    onlineReport.summary.screening_gap = false
    onlineReport.summary.level_distribution = '本次九项核心能力与一项专项能力形成等级。'
    onlineReport.summary.inactive_modules = [
      ['S1b', '本次未出现相应观察情境'],
      ['S2', '本次未出现相应观察情境'],
      ['S3', '本次未出现相应观察情境'],
      ['S4', '本次未出现相应观察情境'],
      ['S5', '本次未出现相应观察情境'],
      ['S6', '本次未出现相应观察情境'],
      ['S7', '本次未出现相应观察情境'],
      ['S8', '本次未出现相应观察情境'],
    ]
    api.getReport.mockResolvedValue(onlineReport)

    const { container } = renderPage()

    expect(await screen.findByRole('heading', {
      name: '初阶心理服务从业者胜任力测评报告',
    })).toBeInTheDocument()
    expect(screen.getByText('场域：在线咨询 · 实时文字')).toBeInTheDocument()
    const summaries = Array.from(container.querySelectorAll('details > summary'))
    for (const summary of summaries) await user.click(summary)
    for (const details of Array.from(container.querySelectorAll('details'))) {
      expect(details).toHaveAttribute('open')
    }

    const c6Card = screen.getByRole('article', { name: '文字表达与互动过程管理' })
    expect(c6Card).toHaveTextContent('文字消息节奏')
    expect(c6Card).toHaveTextContent('文字可理解性')
    expect(container).toHaveTextContent('与对方通话')
    expect(container).toHaveTextContent('心理援助热线')

    const copyWithoutLegitimatePhrases = (container.textContent ?? '')
      .replaceAll('与对方通话', '')
      .replaceAll('心理援助热线', '')
    expect(copyWithoutLegitimatePhrases).not.toMatch(/接线人员|接线员|来电者|来电|通话|热线/)
  })

  it('结果概览只展示三类数量与等级分布，并按中文能力名提供页面导航', async () => {
    renderPage()

    const overviewTitle = await screen.findByRole('heading', { name: '本次结果概览' })
    const overview = overviewTitle.closest('section')
    expect(overview).toHaveTextContent('已形成等级数1 项')
    expect(overview).toHaveTextContent('暂不形成等级数1 项')
    expect(overview).toHaveTextContent('分析未完成数1 项')
    expect(overview).toHaveTextContent('九个核心维度中一个形成等级，一个因无观察机会暂不形成等级，一个分析未完成。')
    expect(overview).not.toHaveTextContent('未评分')
    expect(within(overview!).queryByRole('heading', { name: '下一步可观察行为' })).not.toBeInTheDocument()
    expect(within(overview!).queryByRole('heading', { name: '暂不形成等级的能力' })).not.toBeInTheDocument()
    expect(within(overview!).queryByRole('heading', { name: '分析未完成的能力' })).not.toBeInTheDocument()

    const navigation = screen.getByRole('navigation', { name: '能力结果导航' })
    const links = within(navigation).getAllByRole('link')
    expect(links.map((link) => link.textContent)).toEqual(report.dimensions.map((dimension) => dimension.name))
    links.forEach((link, index) => expect(link).toHaveAttribute('href', `#dimension-${index + 1}`))
  })

  it('概览三类数量只统计九项核心能力，专项能力另行展示', async () => {
    const reportWithFailedModule = structuredClone(report)
    reportWithFailedModule.summary.analysis_failed = ['C3', 'S1a']
    reportWithFailedModule.dimensions[3].result.analysis_outcome = 'analysis_failed'
    reportWithFailedModule.dimensions[3].result.level = null
    api.getReport.mockResolvedValue(reportWithFailedModule)
    renderPage()

    const overviewTitle = await screen.findByRole('heading', { name: '本次结果概览' })
    const overview = overviewTitle.closest('section')!
    expect(overview).toHaveTextContent('分析未完成数1 项')
    expect(overview).toHaveTextContent('以下数量仅统计九项核心能力，专项能力另见后文。')
  })

  it('每项能力说明观察内容，已评分结论层按阅读顺序呈现并限制代表性原话', async () => {
    const user = userEvent.setup()
    renderPage()
    const scored = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })

    for (const dimension of report.dimensions) {
      const card = screen.getByRole('article', { name: dimension.name })
      expect(card).toHaveTextContent('这项能力主要观察什么')
      expect(card).toHaveTextContent(targetDescriptions[dimension.target as keyof typeof targetDescriptions])
    }

    const expectedOrder = [
      within(scored).getByText('本次形成 3 级描述', { selector: 'strong' }),
      within(scored).getByRole('heading', { name: '这一等级表示' }),
      within(scored).getByRole('heading', { name: '本次观察到的表现' }),
      within(scored).getByRole('heading', { name: '为什么这样判断' }),
      within(scored).getByRole('heading', { name: '要进一步判断，还需要看到' }),
      within(scored).getByRole('heading', { name: '代表性原话' }),
    ]
    expectedOrder.slice(1).forEach((element, index) => {
      expect(expectedOrder[index].compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })
    expect(scored).toHaveTextContent('能稳定展现目标行为，并能根据复杂互动调整做法。')
    expect(scored).toHaveTextContent('先承接当事人的处境，再进入必要核对。')
    expect(scored).toHaveTextContent('代表性原话与工作记录共同支持这一判断。')
    expect(scored).toHaveTextContent('说明关键提问的目的，并邀请当事人补充。')
    const representative = within(scored).getByRole('heading', { name: '代表性原话' }).closest('section')!
    expect(within(representative).getAllByRole('button', { name: /查看通话原文/ })).toHaveLength(2)
    expect(representative).toHaveTextContent('听起来这几天非常难熬，我想先确认你现在是否安全。')
    expect(representative).toHaveTextContent('我们一起看看现在能联系谁。')
    expect(representative).not.toHaveTextContent('在挂断前，我们再确认一下接下来怎么做。')

    await user.click(within(scored).getByText('查看评分依据与观察范围'))
    const fullEvidence = within(scored).getByRole('heading', { name: '完整支持证据' }).closest('section')!
    expect(within(fullEvidence).getAllByRole('button', { name: /查看通话原文/ })).toHaveLength(3)
    expect(fullEvidence).toHaveTextContent('在挂断前，我们再确认一下接下来怎么做。')
  })

  it('原始证据逐字保留内部词形，只有分析文字进行用户化表述', async () => {
    const reportWithLiteralQuote = structuredClone(report)
    const literalQuote = '我在记录里写的是 C1、crisis_student_main、未评分。'
    reportWithLiteralQuote.dimensions[0].result.evidence[0].ref.quote = literalQuote
    reportWithLiteralQuote.dimensions[0].result.pattern = '结论涉及 C1、crisis_student_main，目前未评分。'
    api.getReport.mockResolvedValue(reportWithLiteralQuote)
    renderPage()

    const scored = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const representative = within(scored).getByRole('heading', { name: '代表性原话' }).closest('section')!
    const evidenceButton = within(representative).getByRole('button', {
      name: `查看通话原文：${literalQuote}`,
    })
    expect(evidenceButton).toHaveTextContent(literalQuote)
    expect(evidenceButton).toHaveAccessibleName(`查看通话原文：${literalQuote}`)
    expect(scored).toHaveTextContent('结论涉及 尊重、真诚与非评判性沟通、明早她就到了，目前暂不形成等级。')
  })

  it('评分依据默认关闭，展开后显示中文观察情境、指标说明和材料条件', async () => {
    const user = userEvent.setup()
    renderPage()
    const scored = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const details = within(scored).getByText('查看评分依据与观察范围').closest('details')

    expect(details).not.toHaveAttribute('open')
    await user.click(within(scored).getByText('查看评分依据与观察范围'))
    expect(details).toHaveAttribute('open')
    expect(details).toHaveTextContent('本次能够观察到的情境')
    expect(details).toHaveTextContent('尊重与非评判')
    expect(details).toHaveTextContent('观察回应中是否避免责备、羞辱、讽刺或道德评价。')
    expect(details).toHaveTextContent('每次通话都应观察')
    expect(details).toHaveTextContent('仅在相应情境出现时观察')
    expect(details).toHaveTextContent('本次已出现相应情境')
    expect(details).toHaveTextContent('本次未出现相应情境')
    expect(details).toHaveTextContent('包含较复杂情境')
    expect(details).toHaveTextContent('完整支持证据')
    expect(details).toHaveTextContent('完整限制证据')
    expect(details).toHaveTextContent('材料对判断的支持程度')
    expect(details).toHaveTextContent('影响本次等级判断的材料条件')
    expect(details).toHaveTextContent('本次没有出现的条件情境')
    expect(details).not.toHaveTextContent(/C1\.respect|C1\.repair|必需机会|条件机会|已兑现|未兑现/)
  })

  it('把暂不形成等级与分析未完成分开说明，并为两类结果保留折叠依据', async () => {
    renderPage()
    await screen.findByRole('heading', { name: '本次结果概览' })

    const unscored = screen.getByRole('article', { name: '倾听、情绪识别与回应' })
    expect(unscored).toHaveTextContent('本次暂不形成等级')
    expect(unscored).toHaveTextContent('本次没有对应观察机会')
    expect(unscored).toHaveTextContent('这不表示受测者不具备该项能力。')
    expect(unscored).not.toHaveTextContent(/本次形成 \d 级描述/)
    expect(within(unscored).getByText('查看评分依据与观察范围').closest('details')).not.toHaveAttribute('open')

    const failed = screen.getByRole('article', { name: '关切澄清与信息收集' })
    expect(failed).toHaveTextContent('这项能力暂未完成分析')
    expect(failed).toHaveTextContent('原因来自分析过程，不能据此判断受测者的能力或材料质量。')
    expect(failed).not.toHaveTextContent(/材料不足|证据不足|暂不形成等级/)
    expect(within(failed).getByText('查看评分依据与观察范围').closest('details')).not.toHaveAttribute('open')
  })

  it('有异常数据时展示严重问题、筛查缺口和材料冲突，并优先使用报告中的能力名称', async () => {
    renderPage()

    const priorityTitle = await screen.findByRole('heading', { name: '需要优先核对的事项' })
    const priority = priorityTitle.closest('section')!
    expect(within(priority).getByRole('heading', { name: '需要立即核对的严重问题' })).toBeInTheDocument()
    expect(within(priority).getByText('本次未完成基础风险筛查')).toBeInTheDocument()
    const conflicts = within(priority).getByRole('heading', { name: '对话与工作记录不一致之处' }).closest('section')!
    expect(within(conflicts).getByText('是否已联系现实支持者的记载不一致。')).toBeInTheDocument()
    expect(conflicts).toHaveTextContent('尊重、真诚与非评判性沟通')
    expect(conflicts).toHaveTextContent('工作记录的准确性与可追溯性')
  })

  it('没有异常且筛查完成时只显示合并后的核对结论，摘要空栏目不占位', async () => {
    const cleanReport = structuredClone(report)
    cleanReport.bottom_line_events = []
    cleanReport.material_conflicts = []
    cleanReport.screening_gap = false
    cleanReport.summary.next_behaviors = []
    cleanReport.summary.unscored = []
    cleanReport.summary.analysis_failed = []
    api.getReport.mockResolvedValue(cleanReport)
    renderPage()

    const priorityConclusion = await screen.findByText('本次没有需要特别核对的异常事项，基础风险筛查已经完成。')
    const priority = priorityConclusion.closest('section')!
    expect(within(priority).queryByRole('heading', { name: '需要立即核对的严重问题' })).not.toBeInTheDocument()
    expect(within(priority).queryByRole('heading', { name: '基础风险筛查' })).not.toBeInTheDocument()
    expect(within(priority).queryByRole('heading', { name: '对话与工作记录不一致之处' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '下一步可观察行为' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '暂不形成等级的能力' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '分析未完成的能力' })).not.toBeInTheDocument()
  })

  it('按要求组织页面顺序，并折叠未启用专项评估和技术复核记录', async () => {
    renderPage()
    await screen.findByRole('heading', { name: '本次结果概览' })

    const headings = ['本次结果概览', '需要优先核对的事项', '核心能力', '专项能力', '报告如何使用']
      .map((name) => screen.getByRole('heading', { name }))
    headings.slice(1).forEach((heading, index) => {
      expect(headings[index].compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })

    const inactive = screen.getByText('本次未启用的专项评估（2项）').closest('details')
    expect(inactive).not.toHaveAttribute('open')
    expect(inactive).toHaveTextContent('完整风险研判')
    expect(inactive).toHaveTextContent('精神病性体验与现实检验困难处理')

    report.disclaimers.forEach((text) => expect(screen.getByText(text)).toBeInTheDocument())
    const record = screen.getByText('技术复核记录').closest('details')
    expect(record).not.toHaveAttribute('open')
    expect(record).toHaveTextContent('量规校验码')
    expect(record).toHaveTextContent('案例材料校验码')
    expect(record).toHaveTextContent('分析模型校验码')
    expect(record).toHaveTextContent('分析提示校验码')
    expect(record).toHaveTextContent('输入材料校验码')
    expect(record).toHaveTextContent('rubric-v1')
    expect(record).toHaveTextContent('input-v1')
    expect(screen.queryByText(/版本/)).not.toBeInTheDocument()
  })

  it('通话、工作记录和声音材料按钮不暴露内部定位编号，并仍可下钻', async () => {
    const user = userEvent.setup()
    renderPage()

    const c1 = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const representative = within(c1).getByRole('heading', { name: '代表性原话' }).closest('section')!
    const dialogueButton = within(representative).getByRole('button', { name: /查看通话原文.*听起来这几天非常难熬/ })
    expect(dialogueButton).not.toHaveAccessibleName(/turn-2/)
    await user.click(dialogueButton)
    let drawer = await screen.findByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('我最近实在撑不住了。')
    expect(drawer).toHaveTextContent('我还没有联系任何人。')
    expect(within(drawer).getByText('听起来这几天非常难熬，我想先确认你现在是否安全。').closest('li')).toHaveAttribute('aria-current', 'true')
    await user.click(within(drawer).getByRole('button', { name: '关闭原始材料' }))

    await user.click(within(c1).getByText('查看评分依据与观察范围'))
    const workRecordButton = within(c1).getByRole('button', { name: /查看工作记录.*尚未确认危险物品可及性/ })
    expect(workRecordButton).not.toHaveAccessibleName(/unit-2|limitations/)
    await user.click(workRecordButton)
    drawer = await screen.findByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('信息与判断限制')
    expect(drawer).toHaveTextContent('尚未确认危险物品可及性。')
    await user.click(within(drawer).getByRole('button', { name: '关闭原始材料' }))

    const audioButton = screen.getByRole('button', { name: '查看声音材料说明' })
    expect(audioButton).not.toHaveAccessibleName(/audio-1/)
    await user.click(audioButton)
    drawer = await screen.findByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('本次未分析声音表现')
    expect(drawer).not.toHaveTextContent('audio-1')
  })

  it('风险判断证据使用自然入口隐藏话轮编号，并仍能定位通话原文', async () => {
    const user = userEvent.setup()
    const reportWithRiskEvidence = structuredClone(report)
    const firstEvidence = reportWithRiskEvidence.dimensions[0].result.evidence[0] as unknown as { ref: EvidenceRef }
    firstEvidence.ref = {
      kind: 'work_record',
      field: 'risk_evidence_turn_ids',
      quote: 'turn-2',
    }
    api.getReport.mockResolvedValue(reportWithRiskEvidence)
    const { container } = renderPage()

    const c1 = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const representative = within(c1).getByRole('heading', { name: '代表性原话' }).closest('section')!
    const trigger = within(representative).getByRole('button', { name: '查看风险判断原话' })
    expect(trigger).toHaveTextContent('查看风险判断原话')
    expect(trigger).not.toHaveAccessibleName(/turn-2|risk_evidence_turn_ids/)
    expect(container.textContent).not.toContain('turn-2')

    await user.click(trigger)
    const drawer = await screen.findByRole('dialog', { name: '查看原始材料' })
    expect(drawer).toHaveTextContent('引用的通话原文 1 · 接线人员')
    expect(drawer).toHaveTextContent('听起来这几天非常难熬，我想先确认你现在是否安全。')
    expect(drawer).not.toHaveTextContent('turn-2')
  })

  it('证据抽屉打开后约束键盘焦点，Escape 关闭并恢复触发按钮焦点', async () => {
    const user = userEvent.setup()
    renderPage()

    const c1 = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const representative = within(c1).getByRole('heading', { name: '代表性原话' }).closest('section')!
    const trigger = within(representative).getByRole('button', { name: /查看通话原文.*听起来这几天非常难熬/ })
    await user.click(trigger)

    const drawer = await screen.findByRole('dialog', { name: '查看原始材料' })
    const close = within(drawer).getByRole('button', { name: '关闭原始材料' })
    expect(close).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(close).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: '查看原始材料' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('证据上下文按 sessionId 复用整份会话查询，缓存键不含 turnId', async () => {
    const user = userEvent.setup()
    const { queryClient } = renderPage()

    const c1 = await screen.findByRole('article', { name: '尊重、真诚与非评判性沟通' })
    const representative = within(c1).getByRole('heading', { name: '代表性原话' }).closest('section')!
    await user.click(within(representative).getByRole('button', { name: /查看通话原文.*听起来这几天非常难熬/ }))
    await screen.findByText('我最近实在撑不住了。')

    expect(queryClient.getQueryData(['session', 'session-1'])).toBeDefined()
    expect(queryClient.getQueryData(['evidence-context', 'session-1', 'turn-2'])).toBeUndefined()
  })

  it('部分报告可重新分析失败分组并返回任务页', async () => {
    const user = userEvent.setup()
    api.retryReportJob.mockResolvedValue({ id: 'job-1', stage: 'queued' })
    renderPage()

    expect(await screen.findByText('部分维度分析尚未完成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新分析未完成维度' }))

    expect(api.retryReportJob).toHaveBeenCalledWith('job-1')
    expect(await screen.findByText('/report-jobs/job-1')).toBeInTheDocument()
  })
})
