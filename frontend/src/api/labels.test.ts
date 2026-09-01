import { describe, expect, it } from 'vitest'

import {
  caseNames,
  confidenceLabels,
  displayCaseName,
  displayIndicator,
  displayIndicatorForMedia,
  displayTargetDescription,
  displayTargetName,
  displayTargetNameForMedia,
  indicatorDescriptions,
  indicatorNames,
  label,
  plannedActionLabels,
  riskLevelLabels,
  targetDescriptions,
  targetNames,
  workRecordFieldLabels,
} from './labels'

const targets = [
  'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9',
  'S1a', 'S1b', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8',
] as const

const inheritedPropertyNames = ['toString', 'constructor', '__proto__'] as const

const authoritativeTargetNames = {
  C1: '尊重性沟通与关系维护',
  C2: '倾听与情绪理解',
  C3: '来电诉求澄清与工作焦点',
  C4: '信息整合与专业判断',
  C5: '支持性介入与协同行动',
  C6: '语音沟通与通话进程管理',
  C7: '服务边界与伦理责任',
  C8: '通话结束与后续安排',
  C9: '工作记录的准确性与可追溯性',
  S1a: '基础风险筛查',
  S1b: '完整风险研判',
  S2: '高危状态下的安全处置与协作',
  S3: '强烈情绪、长时沉默与表达失序处理',
  S4: '精神病性体验与现实检验困难处理',
  S5: '反复来电、依赖与边界压力处理',
  S6: '攻击、骚扰与非服务性来电处理',
  S7: '第三方来电处理',
  S8: '未成年人或疑似受侵害来电处理',
} as const

describe('报告中文展示元数据', () => {
  it('为所有测评案例提供自然中性的中文名称', () => {
    expect(caseNames).toMatchObject({
      crisis_student_main: '明早她就到了',
      boundary_referral_short: '只想继续找你',
      marriage_boundary_main: '锁屏亮了一下',
    })
    expect(displayCaseName('crisis_student_main')).toBe('明早她就到了')
    expect(displayCaseName('boundary_referral_short')).toBe('只想继续找你')
    expect(displayCaseName('marriage_boundary_main')).toBe('锁屏亮了一下')
  })

  it('为所有 Target 提供完整的中文观察说明', () => {
    expect(Object.keys(targetNames).sort()).toEqual([...targets].sort())
    expect(Object.keys(targetDescriptions).sort()).toEqual([...targets].sort())

    for (const target of targets) {
      expect(targetDescriptions[target], target).toMatch(/^主要看.+。$/)
    }
  })

  it('逐项使用职业胜任力量规的权威 Target 名称', () => {
    expect(targetNames).toEqual(authoritativeTargetNames)
  })

  it('为文字媒介适配全部能力与指标的静态量规文案', () => {
    const targetCopy = targets.flatMap((target) => [
      displayTargetNameForMedia(target, 'text'),
      displayTargetDescription(target, 'text'),
    ])
    const indicatorCopy = Object.keys(indicatorNames).flatMap((indicatorId) => {
      const indicator = displayIndicatorForMedia(indicatorId, 'text')
      return [indicator.name, indicator.description]
    })

    expect([...targetCopy, ...indicatorCopy].join('\n')).not.toMatch(/接线人员|接线员|来电者|来电|通话|热线/)
    expect(displayTargetNameForMedia('C3', 'text')).toBe('求助诉求澄清与工作焦点')
    expect(displayTargetNameForMedia('C6', 'text')).toBe('文字表达与互动过程管理')
    expect(displayTargetNameForMedia('C8', 'text')).toBe('会谈结束与后续安排')
    expect(displayTargetNameForMedia('S5', 'text')).toBe('反复求助、依赖与边界压力处理')
    expect(displayTargetNameForMedia('S6', 'text')).toBe('攻击、骚扰与非服务性互动处理')
    expect(displayTargetNameForMedia('S7', 'text')).toBe('第三方求助处理')
    expect(displayTargetNameForMedia('S8', 'text')).toBe('未成年人或疑似受侵害求助处理')
  })

  it('媒介适配不改变语音量规文案和未知值兜底', () => {
    expect(displayTargetNameForMedia('C6', 'voice')).toBe(targetNames.C6)
    expect(displayTargetDescription('C6', 'voice')).toBe(targetDescriptions.C6)
    expect(displayIndicatorForMedia('C6.clarity', 'voice')).toEqual(displayIndicator('C6.clarity'))
    expect(displayTargetNameForMedia('X99', 'text')).toBe('未命名能力')
  })

  it('准确说明工作记录与特殊保护情形的观察范围', () => {
    expect(targetDescriptions.C9).toBe(
      '主要看工作记录是否准确区分事实与判断，并完整记录依据、行动状态、未知信息和判断限制。',
    )
    expect(targetDescriptions.S8).toBe(
      '主要看接线人员面对未成年人、疑似虐待、性侵害、家庭暴力或其他特殊保护情形时，能否使用适合其年龄与理解能力的沟通方式，了解必要安全事实并落实保护责任。',
    )
  })

  it('为每个指标提供可观察行为说明，且名称与说明的键完全一致', () => {
    expect(Object.keys(indicatorDescriptions).sort()).toEqual(Object.keys(indicatorNames).sort())

    for (const indicatorId of Object.keys(indicatorNames) as Array<keyof typeof indicatorNames>) {
      const name = indicatorNames[indicatorId]
      const description = indicatorDescriptions[indicatorId]
      expect(description, indicatorId).toMatch(/[\u4e00-\u9fff]/)
      expect(description.trim().length, indicatorId).toBeGreaterThan(8)
      expect(description, indicatorId).not.toBe(name)
      expect(displayIndicator(indicatorId)).toEqual({ name, description })
    }
  })

  it('未知案例、Target 和指标只返回自然中文兜底，不泄露内部值', () => {
    const unknownCase = 'internal_case_42'
    const unknownTarget = 'X99'
    const unknownIndicator = 'X99.raw_indicator'
    const displayed = {
      caseName: displayCaseName(unknownCase),
      targetName: displayTargetName(unknownTarget),
      indicator: displayIndicator(unknownIndicator),
    }
    const serialized = JSON.stringify(displayed)

    expect(displayed).toEqual({
      caseName: '未命名测评案例',
      targetName: '未命名能力',
      indicator: {
        name: '其他观察内容',
        description: '这项内容暂未配置中文说明，请结合量规原文核对。',
      },
    })
    expect(serialized).not.toContain(unknownCase)
    expect(serialized).not.toContain(unknownTarget)
    expect(serialized).not.toContain(unknownIndicator)
  })

  it.each(inheritedPropertyNames)('将继承属性 %s 视为未知值并使用中文兜底', (inheritedName) => {
    expect(displayCaseName(inheritedName)).toBe('未命名测评案例')
    expect(displayTargetName(inheritedName)).toBe('未命名能力')
    expect(displayIndicator(inheritedName)).toEqual({
      name: '其他观察内容',
      description: '这项内容暂未配置中文说明，请结合量规原文核对。',
    })
    expect(label({ known: '已知内容' }, inheritedName)).toBe('其他内容')
  })

  it('使用易懂的中文表达材料支持程度', () => {
    expect(confidenceLabels).toEqual({
      high: '支持充分',
      medium: '支持程度一般',
      low: '支持有限',
    })
  })

  it('通用标签保持已知值的中文映射', () => {
    expect(label({ known: '已知内容' }, 'known')).toBe('已知内容')
  })

  it('工作记录使用场域共通的专业文案并收窄无风险结论', () => {
    expect(workRecordFieldLabels).toMatchObject({
      problem_understanding: '本次求助、当前需要与已确认信息',
      risk_reasoning: '当前安全研判及依据',
      risk_evidence_turn_ids: '关键判断与处置的原话依据',
      missing_information: '仍未查明的信息',
      planned_actions: '本次涉及的工作类别',
      referral_decision: '服务衔接判断',
      supervision_decision: '需要负责人或督导进一步讨论',
      follow_up: '行动状态与后续衔接',
      limitations: '信息与判断限制',
    })
    expect(riskLevelLabels.no_identified).toBe('本次未识别到明确的当前风险')
  })

  it('为新增与历史工作类别提供唯一中文标签', () => {
    expect(plannedActionLabels).toMatchObject({
      continue_assessment: '继续澄清与评估',
      stay_connected: '保持连接与陪伴',
      supervisor: '与负责人／督导讨论',
      emotion_stabilization: '情绪稳定',
      goal_clarification: '目标澄清',
      conflict_deescalation: '冲突降温',
      autonomy_support: '自主决策支持',
      resource_linkage: '资源衔接',
    })
  })

  it('通用标签为未知值返回自然中文且不泄露输入原值', () => {
    const rawValue = 'internal_raw'
    const displayed = label({ known: '已知内容' }, rawValue)

    expect(displayed).toBe('其他内容')
    expect(displayed).not.toContain(rawValue)
  })
})
