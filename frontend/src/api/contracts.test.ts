import { describe, expect, it } from 'vitest'

import {
  dimensionResultSchema,
  providerCheckSchema,
  providerConfigSchema,
  reportJobSchema,
  reportSchema,
  rubricDocumentSchema,
  sessionSchema,
  turnSchema,
  workRecordSchema,
} from './contracts'

const codedEvidence = {
  unit_id: 'unit-1',
  target: 'C1',
  indicator_id: 'C1.1',
  direction: 'support',
  strength: 'strong',
  context: '来访者表达危险想法后，受测者先确认当下安全。',
  alternative_reading: null,
  ref: { kind: 'dialogue', turn_id: 'turn-1', quote: '我想先确认你现在是否安全。' },
}

const dimension = {
  target: 'C1',
  name: '尊重、真诚与非评判性沟通',
  level_anchor: '能持续使用尊重、真诚且非评判的表达，并根据互动调整回应。',
  result: {
    target: 'C1', level: 3, unscored_reason: null, analysis_outcome: 'ok',
    opportunities: [],
    indicator_states: { 'C1.1': 'demonstrated' },
    pattern: '能先回应处境，再进入安全核对。',
    rationale: '代表性原话支持该等级判断。',
    evidence: [codedEvidence], counter_evidence: [],
    representative_unit_ids: ['unit-1'], limiting_unit_ids: [],
    conditional_unavailable: [], caps_applied: [], evidence_confidence: 'high',
    evidence_confidence_factors: ['原话与工作记录相互支持'],
    next_level_gap: ['在确认安全后说明提问目的'],
  },
}

const bottomLineEvent = {
  id: 'event-1', category: 'false_confidentiality', detection: 'semantic',
  refs: [{ kind: 'work_record', field: 'limitations', quote: '已承诺所有内容绝不外传。' }],
  description: '保密边界表述不准确。', reasoning: '原记录包含绝对保密承诺。', repair_observed: false,
}

const report = {
  id: 'report-1', session_id: 'session-1', job_id: 'job-1', case_id: 'crisis_student_main',
  scene: 'hotline', media: 'voice',
  summary: {
    scored_core_count: 1, unscored: [['C2', 'no_opportunity']], analysis_failed: ['C3'],
    activated_modules: ['S1a'], inactive_modules: [['S1b', '本个案未声明完整风险评估机会']],
    bottom_line_events: [bottomLineEvent], screening_gap: true,
    level_distribution: '九个核心维度中一个形成等级。',
    next_behaviors: ['在确认安全后说明提问目的'],
  },
  dimensions: [dimension], bottom_line_events: [bottomLineEvent],
  material_conflicts: [{
    id: 'conflict-1',
    dialogue_ref: { kind: 'dialogue', turn_id: 'turn-2', quote: '我还没有联系任何人。' },
    work_record_ref: { kind: 'work_record', field: 'follow_up', quote: '已联系支持者。' },
    description: '对话与工作记录对是否联系支持者的记载不同。',
    affected_targets: ['C9'], impact: 'C9 的记录准确性判断受到限制。',
  }],
  screening_gap: true, disclaimers: ['不得用于胜任判定。', '仅用于发展性反馈。'],
  rubric_fingerprint: 'rubric-v1', case_package_fingerprint: 'case-v1',
  model_fingerprint: 'model-v1', prompt_fingerprint: 'prompt-v1', input_fingerprint: 'input-v1',
  ai_draft_status: 'partial', created_at: '2026-08-30T00:00:00Z',
}

const rubricDocument = {
  title: '心理咨询与危机干预胜任力测评量规',
  markdown: '## C1 尊重、真诚与非评判性沟通\n\n在高风险情境中，先确认当事人当下安全，并说明提问目的。',
}

describe('报告任务与证据报告 API 契约', () => {
  it('只接受包含专业中文标题与正文的评分量规文档', () => {
    expect(rubricDocumentSchema.parse(rubricDocument)).toEqual(rubricDocument)
    expect(rubricDocumentSchema.safeParse({ ...rubricDocument, version: 'v1' }).success).toBe(false)
    expect(rubricDocumentSchema.safeParse({ ...rubricDocument, fingerprint: 'rubric-v1' }).success).toBe(false)
  })

  it.each([
    ['title', ''],
    ['title', '   '],
    ['markdown', ''],
    ['markdown', ' \n\t '],
  ] as const)('拒绝空白量规 %s 字段', (field, value) => {
    expect(rubricDocumentSchema.safeParse({ ...rubricDocument, [field]: value }).success).toBe(false)
  })

  it('严格校验后端报告任务状态与进度', () => {
    const job = reportJobSchema.parse({
      id: 'job-1', session_id: 'session-1', stage: 'scoring', progress_percent: 65,
      partial: false, retryable: false, report_id: null,
      created_at: '2026-08-30T00:00:00Z', updated_at: '2026-08-30T00:01:00Z',
    })

    expect(job.stage).toBe('scoring')
    expect(reportJobSchema.safeParse({ ...job, progress_percent: 101 }).success).toBe(false)
  })

  it('接受三类证据、未评分与分析失败分列，并拒绝旧总分字段口径', () => {
    expect(reportSchema.parse(report).summary.analysis_failed).toEqual(['C3'])
    expect(reportSchema.safeParse({ ...report, raw_score: 24 }).success).toBe(false)

    const audioReport = {
      ...report,
      bottom_line_events: [{ ...bottomLineEvent, refs: [{ kind: 'audio_event', event_id: 'audio-1' }] }],
    }
    expect(reportSchema.parse(audioReport).bottom_line_events[0].refs[0].kind).toBe('audio_event')
  })

  it('公开配置只接受来访者对话、报告与语音模型', () => {
    const parsed = providerConfigSchema.parse({
      configured: true, masked_key: '••••1234', workspace_id: null,
      report_model: 'qwen-max', actor_model: 'qwen-plus-character',
      asr_model: 'qwen-audio-asr', tts_model: 'qwen-audio-tts', tts_voice: 'longanlingxin',
      report_temperature: 0.2, actor_temperature: 0.75,
      actor_context_window_tokens: 32768, actor_max_output_tokens: 2048,
    })

    expect(parsed.report_model).toBe('qwen-max')
    expect(parsed.actor_model).toBe('qwen-plus-character')
    expect(parsed.report_temperature).toBe(0.2)
    expect(parsed.actor_context_window_tokens).toBe(32768)
    expect(parsed.actor_max_output_tokens).toBe(2048)
  })

  it('拒绝服务商配置中未声明的字段', () => {
    const result = providerConfigSchema.safeParse({
      configured: true, masked_key: '•••1234', workspace_id: null,
      report_model: 'qwen-max', actor_model: 'qwen-plus-character',
      asr_model: 'qwen-audio-asr', tts_model: 'qwen-audio-tts', tts_voice: 'longanlingxin',
      report_temperature: 0.2, actor_temperature: 0.75,
      actor_context_window_tokens: 32768, actor_max_output_tokens: 2048,
      legacy_report_model: 'old-model',
    })

    expect(result.success).toBe(false)
  })

  it('拒绝旧 Director 公开字段', () => {
    const result = providerConfigSchema.safeParse({
      configured: true, masked_key: '••••1234', workspace_id: null,
      report_model: 'qwen-max', actor_model: 'qwen-plus-character',
      asr_model: 'qwen-audio-asr', tts_model: 'qwen-audio-tts', tts_voice: 'longanlingxin',
      report_temperature: 0.2, actor_temperature: 0.75,
      actor_context_window_tokens: 32768, actor_max_output_tokens: 2048,
      director_model: 'qwen-plus', director_temperature: 0.15,
    })

    expect(result.success).toBe(false)
  })

  it('严格分开已评分、未评分与分析未完成的结论字段', () => {
    const valid = dimension.result
    const failedWithLevel = {
      ...valid,
      level: 2,
      unscored_reason: null,
      analysis_outcome: 'analysis_failed',
      indicator_states: {}, pattern: '', rationale: '', evidence: [], counter_evidence: [],
      representative_unit_ids: [], limiting_unit_ids: [], caps_applied: [],
      evidence_confidence: null, evidence_confidence_factors: [], next_level_gap: [],
    }
    const okWithoutOutcome = {
      ...valid,
      level: null,
      unscored_reason: null,
    }
    const unscoredWithConclusion = {
      ...valid,
      level: null,
      unscored_reason: 'no_opportunity',
    }

    expect(dimensionResultSchema.safeParse(failedWithLevel).success).toBe(false)
    expect(dimensionResultSchema.safeParse(okWithoutOutcome).success).toBe(false)
    expect(dimensionResultSchema.safeParse(unscoredWithConclusion).success).toBe(false)
  })

  it('保留每个维度的必需与条件观察机会事实', () => {
    const parsed = dimensionResultSchema.safeParse({
      ...dimension.result,
      opportunities: [{
        declared_target: 'C1',
        kind: 'conditional',
        fulfilled: true,
        indicator_ids: ['C1.repair'],
        complex_opportunity: true,
      }],
    })

    expect(parsed.success).toBe(true)
    if (parsed.success) {
      expect(parsed.data.opportunities[0]).toEqual(expect.objectContaining({
        kind: 'conditional', fulfilled: true, complex_opportunity: true,
      }))
    }
  })

  it('继续严格校验工作记录枚举', () => {
    expect(workRecordSchema.safeParse({
      id: 'wr-1', session_id: 'session-1', problem_understanding: '理解', risk_level: 'low', risk_reasoning: '依据',
      risk_evidence_turn_ids: ['turn-1'], missing_information: [], planned_actions: ['follow_up'], referral_decision: 'consider',
      supervision_decision: false, follow_up: '随访', limitations: '限制', created_at: 'now', updated_at: 'now',
    }).success).toBe(true)
  })

  it('工作记录接受新增工作类别且继续兼容全部旧值', () => {
    const base = {
      id: 'wr-1', session_id: 'session-1', problem_understanding: '理解', risk_level: 'low', risk_reasoning: '依据',
      risk_evidence_turn_ids: [], missing_information: [], referral_decision: 'consider',
      supervision_decision: false, follow_up: '衔接', limitations: '限制', created_at: 'now', updated_at: 'now',
    }
    const legacy = [
      'continue_assessment', 'stay_connected', 'contact_support', 'reduce_access',
      'supervisor', 'emergency_services', 'referral', 'follow_up',
    ]
    const additions = [
      'emotion_stabilization', 'goal_clarification', 'conflict_deescalation',
      'autonomy_support', 'resource_linkage',
    ]

    expect(workRecordSchema.safeParse({ ...base, planned_actions: legacy }).success).toBe(true)
    expect(workRecordSchema.safeParse({ ...base, planned_actions: additions }).success).toBe(true)
  })
})

describe('实时测评 API 契约', () => {
  it('保留用于编组原话证据的客户端话轮标识', () => {
    const turn = turnSchema.parse({
      id: 'turn-worker-1', sequence: 2, speaker: 'worker', text: '你现在身边有人吗？',
      client_turn_id: 'voice-1', provider: null, degraded: false, created_at: 'now', audio_available: false,
    })
    expect(turn.client_turn_id).toBe('voice-1')
  })

  it('接受三项正式链服务检查结果和技术中断结束原因', () => {
    expect(providerCheckSchema.parse({
      actor: { status: 'passed', message: null },
      asr: { status: 'failed', message: '实时语音识别暂时无法连接' }, tts: { status: 'passed', message: null },
    }).asr.status).toBe('failed')

    expect(sessionSchema.parse({
      id: 's1', mode: 'assessment', scene: 'hotline', case_type: 'main', case_id: 'case-1',
      media: 'voice', status: 'ended', model_mode: 'live', soft_duration_minutes: 15,
      created_at: 'now', updated_at: 'now', ended_at: 'now', end_reason: 'technical_interruption',
    }).end_reason).toBe('technical_interruption')
  })
})
