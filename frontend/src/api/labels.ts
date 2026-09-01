/** 界面上一律显示中文标签，接口的枚举原值不直接呈现给用户。 */

import type { RuntimePhase, Target } from './contracts'

export const livePhaseLabels: Record<RuntimePhase, string> = {
  listening: '通话中',
  directing: '来访者正在回应',
  acting: '来访者正在回应',
  synthesizing: '来访者正在回应',
  playing: '来访者正在说话',
  technical_paused: '通话暂时中断',
  ended: '本次通话已结束',
}

export function endStateCopy(reason: string | null) {
  if (reason === 'natural_closure') {
    return {
      title: '本次通话已自然结束',
      detail: '来访者已经结束通话，本次会谈原文已完整保留。',
    }
  }
  if (reason === 'technical_interruption') {
    return {
      title: '通话因信号中断结束',
      detail: '已经确认的会谈原文仍会保留，请按实际情况完成工作记录。',
    }
  }
  return {
    title: '你已结束本次通话',
    detail: '本次会谈原文已完整保留，可以继续填写工作记录。',
  }
}

export const caseNames: Record<string, string> = {
  crisis_student_main: '明早她就到了',
  boundary_referral_short: '只想继续找你',
  marriage_boundary_main: '锁屏亮了一下',
}

export const displayCaseName = (caseId: string): string => (
  Object.hasOwn(caseNames, caseId) ? caseNames[caseId] : '未命名测评案例'
)

export const targetNames: Record<Target, string> = {
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
}

const textMediaTargetNames: Partial<Record<Target, string>> = {
  C3: '求助诉求澄清与工作焦点',
  C6: '文字表达与互动过程管理',
  C8: '会谈结束与后续安排',
  S5: '反复求助、依赖与边界压力处理',
  S6: '攻击、骚扰与非服务性互动处理',
  S7: '第三方求助处理',
  S8: '未成年人或疑似受侵害求助处理',
}

/** 只适配本地固定量规目录；模型分析和证据原话不经过这里。 */
const textMediaStaticCopyReplacements = [
  ['热线可以继续提供服务', '当前互动可以继续'],
  ['热线服务和专业关系边界', '当前服务范围和专业关系边界'],
  ['热线服务范围', '当前服务范围'],
  ['热线可提供', '当前服务可提供'],
  ['一次热线', '一次支持互动'],
  ['热线能力限制', '当前服务能力限制'],
  ['热线交流', '当前交流'],
  ['热线使用方式', '服务使用方式'],
  ['热线行动范围', '当前服务的行动范围'],
  ['接线人员', '受测者'],
  ['接线员', '受测者'],
  ['来电者', '来访者'],
  ['非服务性来电', '非服务性互动'],
  ['反复来电', '反复求助'],
  ['第三方来电', '第三方求助'],
  ['来电', '求助'],
  ['通话', '会谈'],
  ['热线', '当前服务'],
] as const

function adaptStaticRubricCopy(value: string, media: 'voice' | 'text'): string {
  if (media === 'voice') return value
  return textMediaStaticCopyReplacements.reduce(
    (copy, [source, replacement]) => copy.replaceAll(source, replacement),
    value,
  )
}

export const targetDescriptions: Record<Target, string> = {
  C1: '主要看接线人员能否以尊重、平等、非评判的方式回应来电者，并在关系出现紧张时及时调整。',
  C2: '主要看接线人员能否听懂来电者正在说什么、感受到什么，并通过核对修正自己的理解。',
  C3: '主要看接线人员能否从零散叙述中澄清来电缘由、当前需要和本次通话最需要处理的重点。',
  C4: '主要看接线人员能否区分事实、推测和未知信息，并据此形成有边界、可调整的专业判断。',
  C5: '主要看接线人员能否根据来电者的状态、意愿和现实条件提供合适的支持，并共同确定可行行动。',
  C6: '主要看接线人员的表达是否清楚，能否留出回应空间，并使通话节奏和结构便于来电者继续交流。',
  C7: '主要看接线人员能否守住热线服务范围、保密边界和专业关系，并如实说明自己已做和未做的事情。',
  C8: '主要看接线人员能否选择合适时机结束通话，完成回顾、状态确认、行动确认和后续衔接。',
  C9: '主要看工作记录是否准确区分事实与判断，并完整记录依据、行动状态、未知信息和判断限制。',
  S1a: '主要看接线人员是否在每次通话中以清楚、不过度诱导的方式完成基础风险筛查，并根据回答继续追问。',
  S1b: '主要看接线人员能否识别风险线索，直接询问关键风险信息，并结合保护因素和未知信息作出审慎研判。',
  S2: '主要看接线人员在出现现实安全风险时，能否保持连接、核实现实条件并推动可执行的安全行动。',
  S3: '主要看接线人员面对持续哭泣、明显失控或长时沉默时，能否降低交流负荷、提供稳定支持并恢复有效沟通。',
  S4: '主要看接线人员面对异常知觉或思维体验时，能否回应当事人的真实感受，评估影响并作出适当服务判断。',
  S5: '主要看接线人员面对反复来电、依赖或关系压力时，能否理解其需要、清楚说明边界并安排可持续支持。',
  S6: '主要看接线人员面对攻击、骚扰或威胁时，能否保持稳定，说明最低互动条件，并妥善结束和记录。',
  S7: '主要看接线人员处理第三方来电时，能否核对身份与目的，守住信息边界，并把注意力放在可采取的帮助行动上。',
  S8: '主要看接线人员面对未成年人、疑似虐待、性侵害、家庭暴力或其他特殊保护情形时，能否使用适合其年龄与理解能力的沟通方式，了解必要安全事实并落实保护责任。',
}

const textMediaTargetDescriptions: Partial<Record<Target, string>> = {
  C6: '主要看受测者能否写出清楚、容易跟随的消息，控制连续追问负荷，并根据来访者的反馈调整文字消息节奏、澄清方式和响应节奏。',
  C7: '主要看受测者能否守住当前服务的范围、隐私与保密边界和专业关系，并如实说明自己已做和未做的事情。',
  C8: '主要看受测者能否选择合适时机结束互动，完成回顾、状态确认、行动确认和后续衔接。',
}

export function displayTargetDescription(target: Target, media: 'voice' | 'text'): string {
  if (media === 'voice') return targetDescriptions[target]
  return textMediaTargetDescriptions[target]
    ?? adaptStaticRubricCopy(targetDescriptions[target], media)
}

export const displayTargetName = (target: string): string => (
  Object.hasOwn(targetNames, target) ? targetNames[target as Target] : '未命名能力'
)

export function displayTargetNameForMedia(target: string, media: 'voice' | 'text'): string {
  if (!Object.hasOwn(targetNames, target)) return '未命名能力'
  const knownTarget = target as Target
  if (media === 'voice') return targetNames[knownTarget]
  return textMediaTargetNames[knownTarget]
    ?? adaptStaticRubricCopy(targetNames[knownTarget], media)
}

/** 只保留量规对外显示的指标名，不包含案例隐藏描述或观察答案。 */
export const indicatorNames = {
  'C1.respect': '尊重与非评判',
  'C1.equal_stance': '平等互动姿态',
  'C1.autonomy': '表达自主性',
  'C1.rupture_detection': '关系紧张识别',
  'C1.repair': '关系修复',
  'C2.content_tracking': '内容跟随',
  'C2.emotion_recognition': '情绪识别',
  'C2.situated_understanding': '处境性理解',
  'C2.verification': '核对与修正',
  'C2.ambivalence': '矛盾体验容纳',
  'C3.call_reason': '来电缘由澄清',
  'C3.current_need': '当前需要辨别',
  'C3.prioritization': '重点与优先次序',
  'C3.shared_focus': '共同确认焦点',
  'C3.focus_adjustment': '焦点调整',
  'C4.relevance': '信息相关性',
  'C4.integration': '信息关联',
  'C4.evidence_boundary': '证据边界',
  'C4.judgment': '判断与优先次序',
  'C4.hypothesis_revision': '假设修正',
  'C5.fit': '介入匹配',
  'C5.resources': '既有应对与资源利用',
  'C5.timing': '建议时机与方式',
  'C5.shared_choice': '共同选择',
  'C5.feedback_adjustment': '反馈与调整',
  'C5.action_layers': '行动层次',
  'C6.clarity': '口语可理解性',
  'C6.turn_space': '话轮与回应空间',
  'C6.cue_adaptation': '交流线索适配',
  'C6.interruption_handling': '沉默与交流中断处理',
  'C6.structure': '通话结构',
  'C6.time_use': '时间使用',
  'C7.role_scope': '角色与服务范围',
  'C7.privacy': '隐私与保密',
  'C7.relationship_boundary': '专业关系边界',
  'C7.informed_participation': '知情参与与自主',
  'C7.competence_scope': '胜任力与责任范围',
  'C7.integrity': '信息与行动诚信',
  'C8.timing': '结束时机',
  'C8.notice': '结束预告',
  'C8.review': '共同回顾',
  'C8.status_action': '状态与行动确认',
  'C8.continuity': '连续性安排',
  'C8.caller_ending': '主动结束应对',
  'C9.fact_accuracy': '事实准确',
  'C9.source_distinction': '来源区分',
  'C9.traceability': '判断可追溯',
  'C9.action_state': '行动状态区分',
  'C9.limitations': '缺失与限制',
  'C9.professional_language': '专业表达',
  'S1a.screening_scope': '筛查覆盖',
  'S1a.wording': '提问方式',
  'S1a.timing': '时机适当',
  'S1a.followup': '回应跟进',
  'S1a.denial_handling': '否认结果处理',
  'S1b.cue_recognition': '线索识别',
  'S1b.direct_question': '直接询问',
  'S1b.urgency': '紧迫性信息',
  'S1b.risk_protection': '风险与保护因素',
  'S1b.appraisal': '综合研判',
  'S1b.limitations': '限制说明',
  'S2.connection': '持续连接',
  'S2.real_world_safety': '现实安全条件',
  'S2.reduce_access': '危险可及性降低',
  'S2.transparent_collaboration': '透明协作',
  'S2.escalation': '升级与交接',
  'S2.verification': '行动核实',
  'S3.state_detection': '状态辨别',
  'S3.load_reduction': '负荷降低',
  'S3.silence_tolerance': '沉默容纳',
  'S3.stabilization': '稳定支持',
  'S3.work_recovery': '恢复工作',
  'S3.safety_attention': '安全留意',
  'S4.experience_response': '体验回应',
  'S4.impact': '影响评估',
  'S4.communication': '沟通适配',
  'S4.service_judgment': '服务判断',
  'S4.referral': '信息与转介',
  'S5.pattern': '模式识别',
  'S5.boundary': '边界说明',
  'S5.dependency': '依赖控制',
  'S5.continuity': '连续性回顾',
  'S5.alternatives': '替代安排',
  'S5.relationship_pressure': '关系压力处理',
  'S6.behavior_detection': '行为辨别',
  'S6.stable_response': '稳定回应',
  'S6.minimum_conditions': '最低互动条件',
  'S6.adjustment': '调整机会',
  'S6.closure_record': '结束与记录',
  'S7.identity_purpose': '身份与目的',
  'S7.evidence_boundary': '证据边界',
  'S7.safety': '安全辨别',
  'S7.actionable_focus': '可行动焦点',
  'S7.information_boundary': '信息边界',
  'S7.help_path': '求助路径',
  'S8.development_fit': '发展适配',
  'S8.necessary_facts': '必要事实',
  'S8.current_safety': '当前安全',
  'S8.confidentiality_protection': '保密与保护说明',
  'S8.role_responsibility': '角色与责任',
  'S8.protection_resources': '保护资源',
} as const satisfies Record<string, string>

/** 说明报告复核层实际观察的行为，不包含案例隐藏信息或标准答案。 */
export const indicatorDescriptions: Record<keyof typeof indicatorNames, string> = {
  'C1.respect': '观察回应中是否避免责备、羞辱、讽刺或道德评价。',
  'C1.equal_stance': '观察是否以平等姿态交流，避免盘问、说教或单方面纠正。',
  'C1.autonomy': '观察是否尊重来电者暂不回答、停顿或纠正理解的权利，不强迫其披露。',
  'C1.rupture_detection': '观察能否发现来电者明显的不满、警惕、退缩、误解或顺从性应付。',
  'C1.repair': '观察回应不当或出现误解后，能否承认具体问题、调整方式并重新建立合作。',
  'C2.content_tracking': '观察能否跟随来电者正在表达的事情，不跳过主线、误置时间或以次要细节代替主要内容。',
  'C2.emotion_recognition': '观察能否识别来电者明确或含蓄表达的主要情绪，而非只复述事件或笼统概括。',
  'C2.situated_understanding': '观察能否结合具体事件、关系、损失、威胁或两难理解情绪，避免脱离处境贴标签。',
  'C2.verification': '观察能否用试探性表达核对理解，并在来电者纠正后及时调整。',
  'C2.ambivalence': '观察能否同时容纳来电者并存的愿望、顾虑和情绪，不把复杂体验简化为单一原因。',
  'C3.call_reason': '观察能否了解促使来电者此时求助的直接原因和近期变化。',
  'C3.current_need': '观察能否分辨来电者是在倾诉、寻求信息、需要稳定情绪、希望解决问题，还是面临安全困难。',
  'C3.prioritization': '观察多个问题同时出现时，能否辨别最紧迫、影响最大或本次最可处理的部分。',
  'C3.shared_focus': '观察能否通过总结或核对与来电者共同确认工作重点，而非单方面规定话题。',
  'C3.focus_adjustment': '观察新的重要信息出现后能否重新排序，不因原有计划忽略当前变化。',
  'C4.relevance': '观察能否围绕当前问题了解必要信息，避免漫无目的收集背景或遗漏影响判断的方面。',
  'C4.integration': '观察能否把诱发事件、主观体验、行为反应、生活功能、既有应对和现实支持联系起来。',
  'C4.evidence_boundary': '观察能否区分来电者原话、可确认事实、专业推断和仍未知的信息。',
  'C4.judgment': '观察能否说明当前最值得关注的问题及理由，并使判断强度与已有证据相称。',
  'C4.hypothesis_revision': '观察遇到矛盾、纠正或新事实后能否调整原有判断，不执着于单一解释。',
  'C5.fit': '观察支持方式是否符合已确认的问题、情绪强度、认知状态和当前通话阶段。',
  'C5.resources': '观察能否了解并利用来电者已有的有效经验、个人能力和现实支持。',
  'C5.timing': '观察是否在充分理解后再提供建议、信息或方法，避免过早、过量或脱离处境。',
  'C5.shared_choice': '观察能否提供有限而清楚的选择，讨论顾虑、偏好和可行性，不替来电者作非必要决定。',
  'C5.feedback_adjustment': '观察能否询问支持是否合适，并在遭到拒绝、效果不佳或实施困难时作出调整。',
  'C5.action_layers': '观察能否区分眼下行动与通话后的安排，并明确行动内容和责任主体。',
  'C6.clarity': '观察表达是否简明、指向清楚，避免过多术语、长篇解释和含糊指令。',
  'C6.turn_space': '观察能否让来电者完成表达，控制单次提问或说明的负荷，避免抢话和连续堆叠问题。',
  'C6.cue_adaptation': '观察能否根据停顿、迟疑、打断、重叠和言语内容等可靠线索调整语速与表达密度。',
  'C6.interruption_handling': '观察能否区分普通停顿、思考、情绪停顿和技术中断，并适当确认和恢复交流。',
  'C6.structure': '观察开场、探索、工作和收束阶段是否清楚，能否用简短总结与过渡维持方向。',
  'C6.time_use': '观察能否在有限时间内保持重点，既不仓促推进，也不长期停留在重复内容中。',
  'C7.role_scope': '观察是否准确理解热线可提供的支持和不能承担的任务，不把一次热线包装成诊断、治疗或长期照护。',
  'C7.privacy': '观察相关情形下能否准确说明保密、必要例外和信息用途，不作绝对保密承诺。',
  'C7.relationship_boundary': '观察是否守住专业关系，不建立私人、排他性、经济性联系，也不以自我披露转移重点。',
  'C7.informed_participation': '观察重要评估、资源联系或升级处置前能否说明目的，让来电者表达顾虑并参与决定。',
  'C7.competence_scope': '观察能否识别自身和热线能力限制，并在需要时提出适当资源、机构流程或进一步服务。',
  'C7.integrity': '观察是否如实说明资源效果和行动状态，不承诺无法完成的事项，也不把计划说成已经执行。',
  'C8.timing': '观察能否结合通话目标、来电者状态、未完成事项和时间条件判断是否适合结束。',
  'C8.notice': '观察能否自然、明确地提示通话即将结束，避免突然中止。',
  'C8.review': '观察能否简要回顾已谈清的内容、仍未解决的问题和本次通话的实际进展。',
  'C8.status_action': '观察结束前能否确认来电者当前是否能够结束通话，以及下一步由谁、何时、做什么。',
  'C8.continuity': '观察能否按需说明再次来电、其他服务、现实支持或紧急求助路径，不作无法兑现的复联承诺。',
  'C8.caller_ending': '观察来电者准备挂断、拒绝继续或突然中止时，能否在现有时间内完成必要回应和记录。',
  'C9.fact_accuracy': '观察工作记录是否与实际对话相符，没有遗漏或改写影响判断的关键信息。',
  'C9.source_distinction': '观察记录能否区分来电者原话、客观通话事实、接线人员判断和尚未核实的信息。',
  'C9.traceability': '观察重要结论是否标明对应通话依据，使读者能够回到原始材料核对。',
  'C9.action_state': '观察记录能否清楚区分已经讨论、已经同意、已经完成、计划采取和未能完成的行动。',
  'C9.limitations': '观察记录是否说明未询问、未获回答、技术影响或通话中断造成的信息缺失和判断限制。',
  'C9.professional_language': '观察记录用语是否中性、具体、克制，避免污名化、文学化、推测性和模板化表述。',
  'S1a.screening_scope': '观察通话中是否触及死亡、自杀或自伤想法，不因来电者未主动提及而跳过。',
  'S1a.wording': '观察是否使用直接、平静、可回答的措辞，不用委婉暗示、诱导性否定或道德化表达代替提问。',
  'S1a.timing': '观察是否在建立基本联系后、通话结束前完成筛查，避免在开场机械插入或收束时敷衍带过。',
  'S1a.followup': '观察能否回应来电者的回答，并在出现肯定或含糊回答时继续了解。',
  'S1a.denial_handling': '观察来电者否认风险时能否如实记录，不把一次否认直接等同于没有风险。',
  'S1b.cue_recognition': '观察能否识别直接、间接和情境性的风险线索。',
  'S1b.direct_question': '观察能否直接询问死亡、自杀或自伤想法，不以委婉暗示代替关键问题。',
  'S1b.urgency': '观察能否根据回答了解意图、计划、方法可及性、时间、既往行为和近期变化。',
  'S1b.risk_protection': '观察能否了解绝望、冲动、物质使用、独处、现实支持以及保护因素。',
  'S1b.appraisal': '观察能否综合正反证据判断紧迫程度，不因主动来电、否认计划或存在保护因素就断定安全。',
  'S1b.limitations': '观察专业判断是否保留尚未获得的信息和判断限制。',
  'S2.connection': '观察必要时能否保持连接，避免来电者在高危状态下独自中断。',
  'S2.real_world_safety': '观察能否确认来电者当前位置、是否独处以及能够及时到场的现实支持。',
  'S2.reduce_access': '观察能否与来电者协商降低危险手段的可及性，或离开危险环境。',
  'S2.transparent_collaboration': '观察能否说明收集信息和升级处置的目的，并回应来电者对失去控制、保密和求助后果的担忧。',
  'S2.escalation': '观察能否根据紧迫程度使用机构规定的督导、紧急服务或其他升级路径。',
  'S2.verification': '观察能否核实安全行动是否真正同意、开始或完成，不以空泛保证代替实际安排。',
  'S3.state_detection': '观察能否判断当前是情绪停顿、思考、拒绝、交流能力下降还是技术中断。',
  'S3.load_reduction': '观察能否降低语言和问题负荷，使用简短、可回应的表达维持联系。',
  'S3.silence_tolerance': '观察能否容纳必要沉默，不因自身焦虑而连续讲话填满空间。',
  'S3.stabilization': '观察能否根据来电者状态采用适当的稳定方法，并说明正在做什么。',
  'S3.work_recovery': '观察状态稍有恢复后，能否重新确认当前需要和通话方向，而非急于追补全部背景。',
  'S3.safety_attention': '观察强烈失控时能否同时留意需要进一步风险评估或安全处置的信息。',
  'S4.experience_response': '观察能否在不嘲讽、不争辩的同时，也不把明显不可靠的信念直接确认为事实。',
  'S4.impact': '观察能否了解异常体验对恐惧、行为、睡眠、生活功能和安全的影响。',
  'S4.communication': '观察能否使用简单、具体、可跟随的语言维持交流。',
  'S4.service_judgment': '观察能否区分可以继续热线交流、需要现实支持以及需要医疗或紧急服务的情况。',
  'S4.referral': '观察提供信息或转介时能否避免污名化，并确认来电者能够理解和执行。',
  'S5.pattern': '观察能否理解反复来电背后的情绪、功能和现实需要，不把来电者简单视为麻烦。',
  'S5.boundary': '观察能否在保持尊重的同时，清楚说明热线服务和专业关系边界。',
  'S5.dependency': '观察是否避免用特殊承诺、私人联系或无限延长通话来缓解眼前压力。',
  'S5.continuity': '观察能否回顾既往讨论和已尝试行动，避免每次从头重复或机械驱赶。',
  'S5.alternatives': '观察能否与来电者形成可持续的热线使用方式、其他支持或长期服务安排。',
  'S5.relationship_pressure': '观察面对愤怒、失望和被抛弃感时，能否维护关系而不取消必要边界。',
  'S6.behavior_detection': '观察能否区分痛苦状态下的激烈表达与持续针对接线人员的攻击或骚扰。',
  'S6.stable_response': '观察能否保持冷静、简短和清楚，不以羞辱、争吵或报复回应。',
  'S6.minimum_conditions': '观察能否清楚说明热线可以继续提供服务的最低互动条件。',
  'S6.adjustment': '观察能否在适当时给予一次明确的调整机会，并说明继续不当行为的后果。',
  'S6.closure_record': '观察能否按机构规则结束不适合继续的通话，并准确记录行为和处理过程。',
  'S7.identity_purpose': '观察能否澄清来电者与当事人的关系、来电目的和目前掌握的信息。',
  'S7.evidence_boundary': '观察是否避免把第三方描述直接当作对未在场者的诊断或完整评估。',
  'S7.safety': '观察能否了解是否存在需要立即处理的人身安全问题。',
  'S7.actionable_focus': '观察能否把重点放在来电者能够采取的支持、沟通和求助行动上。',
  'S7.information_boundary': '观察能否准确说明信息、隐私和热线行动范围，不泄露其他服务对象的信息。',
  'S7.help_path': '观察必要时能否说明如何鼓励当事人本人求助，或如何使用现实紧急资源。',
  'S8.development_fit': '观察能否使用符合来电者年龄、理解能力和当前情绪的语言。',
  'S8.necessary_facts': '观察能否在不诱导、不审讯的前提下了解必要的安全事实。',
  'S8.current_safety': '观察能否识别来电者当下是否仍与可能的伤害者共同生活或处于即时危险。',
  'S8.confidentiality_protection': '观察能否说明保密边界、必要信息用途和可能的保护行动，避免作出无法兑现的承诺。',
  'S8.role_responsibility': '观察能否区分来电者意愿、监护人角色和机构保护责任。',
  'S8.protection_resources': '观察能否按模拟机构配置的程序讨论现实支持、医疗、保护或紧急资源。',
}

export type IndicatorDisplay = {
  name: string
  description: string
}

const unknownIndicatorDisplay: IndicatorDisplay = {
  name: '其他观察内容',
  description: '这项内容暂未配置中文说明，请结合量规原文核对。',
}

export function displayIndicator(indicatorId: string): IndicatorDisplay {
  if (!Object.hasOwn(indicatorNames, indicatorId)) return unknownIndicatorDisplay

  const key = indicatorId as keyof typeof indicatorNames
  return {
    name: indicatorNames[key],
    description: indicatorDescriptions[key],
  }
}

const textMediaIndicatorDisplays: Partial<Record<string, IndicatorDisplay>> = {
  'C6.clarity': {
    name: '文字可理解性',
    description: '观察消息是否简明、指向清楚，避免过多术语、长段解释和含糊指令。',
  },
  'C6.turn_space': {
    name: '消息负荷与回应空间',
    description: '观察能否控制单条消息和连续追问负荷，让来访者有足够空间阅读并回应。',
  },
  'C6.cue_adaptation': {
    name: '文字线索适配',
    description: '观察能否根据短句、修改、答非所问等文字线索调整表达和澄清方式。',
  },
  'C6.interruption_handling': {
    name: '澄清与响应节奏',
    description: '观察信息不完整或回应暂未到达时，能否先核对当前理解，再决定是否继续发送消息。',
  },
  'C6.structure': {
    name: '文字互动结构',
    description: '观察能否让文字交流保持清楚主线，并在转换重点时作出简短说明。',
  },
  'C6.time_use': {
    name: '回应节奏与重点',
    description: '观察能否把有限时间用于当前重点，避免连续发送低价值或重复消息。',
  },
  'C7.role_scope': {
    name: '角色与服务范围',
    description: '观察能否说明当前服务可以提供什么支持，以及哪些事项需要其他专业或现实资源处理。',
  },
  'C7.privacy': {
    name: '隐私与保密',
    description: '观察能否准确说明隐私、保密及其例外，不作无法兑现的绝对承诺。',
  },
  'C7.relationship_boundary': {
    name: '专业关系边界',
    description: '观察能否保持恰当的专业关系，不以私人联系或特殊承诺替代服务安排。',
  },
  'C7.informed_participation': {
    name: '知情参与与自主',
    description: '观察能否说明建议和信息收集的目的，并尊重来访者作出选择。',
  },
  'C7.competence_scope': {
    name: '胜任力与责任范围',
    description: '观察遇到超出当前服务能力的事项时，能否说明限制并使用合适的协作或转介路径。',
  },
  'C7.integrity': {
    name: '信息与行动诚信',
    description: '观察能否如实区分已经完成、正在进行和仍待确认的事项。',
  },
  'C8.timing': {
    name: '结束时机',
    description: '观察能否依据当前重点、来访者状态和已形成的安排选择合适的结束时机。',
  },
  'C8.notice': {
    name: '结束预告',
    description: '观察能否提前说明互动即将结束，并给来访者补充重要内容的机会。',
  },
  'C8.review': {
    name: '共同回顾',
    description: '观察能否与来访者简要回顾已澄清的重点和本次形成的理解。',
  },
  'C8.status_action': {
    name: '状态与行动确认',
    description: '观察结束前能否确认来访者当前状态、行动意愿和现实可行性。',
  },
  'C8.continuity': {
    name: '连续性安排',
    description: '观察能否说明后续支持、转介或再次求助的可行路径。',
  },
  'C8.caller_ending': {
    name: '主动结束应对',
    description: '观察来访者主动结束互动时，能否尊重其决定并完成必要的安全与后续确认。',
  },
}

export function displayIndicatorForMedia(
  indicatorId: string,
  media: 'voice' | 'text',
): IndicatorDisplay {
  if (media === 'voice') return displayIndicator(indicatorId)
  const override = textMediaIndicatorDisplays[indicatorId]
  if (override) return override
  const indicator = displayIndicator(indicatorId)
  return {
    name: adaptStaticRubricCopy(indicator.name, media),
    description: adaptStaticRubricCopy(indicator.description, media),
  }
}

export const workRecordFieldLabels: Record<string, string> = {
  problem_understanding: '本次求助、当前需要与已确认信息',
  risk_level: '当前安全研判等级',
  risk_reasoning: '当前安全研判及依据',
  risk_evidence_turn_ids: '关键判断与处置的原话依据',
  missing_information: '仍未查明的信息',
  planned_actions: '本次涉及的工作类别',
  referral_decision: '服务衔接判断',
  supervision_decision: '需要负责人或督导进一步讨论',
  follow_up: '行动状态与后续衔接',
  limitations: '信息与判断限制',
}

export const riskLevelLabels: Record<string, string> = {
  no_identified: '本次未识别到明确的当前风险',
  low: '低风险',
  moderate: '中等风险',
  high: '高风险',
  imminent: '紧迫风险',
  uncertain: '信息不足，暂无法判断',
}

export const plannedActionLabels: Record<string, string> = {
  continue_assessment: '继续澄清与评估',
  stay_connected: '保持连接与陪伴',
  contact_support: '联系现实支持',
  reduce_access: '降低危险可及性',
  supervisor: '与负责人／督导讨论',
  emergency_services: '协调紧急服务',
  referral: '安排转介',
  follow_up: '安排后续联系',
  emotion_stabilization: '情绪稳定',
  goal_clarification: '目标澄清',
  conflict_deescalation: '冲突降温',
  autonomy_support: '自主决策支持',
  resource_linkage: '资源衔接',
}

export const referralDecisionLabels: Record<string, string> = {
  not_needed: '暂不转介',
  consider: '需进一步评估',
  recommended: '建议转介',
  urgent: '紧急衔接',
}

export const unscoredReasonLabels: Record<string, string> = {
  no_opportunity: '本次没有对应观察机会',
  insufficient_evidence: '现有可靠材料不足以形成等级判断',
  technical_failure: '材料采集受到技术条件限制',
}

export const indicatorStatusLabels: Record<string, string> = {
  demonstrated: '已展现', partial: '部分展现', opportunity_missed: '有机会但未展现',
  adverse: '出现反向行为', no_opportunity: '无观察机会', no_reliable_material: '无可靠材料',
}

export const capReasonLabels: Record<string, string> = {
  adverse_evidence: '存在反向证据',
  conditional_opportunity_unavailable: '条件行为缺少观察机会',
  no_complex_opportunity: '缺少复杂情境观察机会',
}

export const confidenceLabels: Record<string, string> = {
  high: '支持充分', medium: '支持程度一般', low: '支持有限',
}

export const bottomLineCategoryLabels: Record<string, string> = {
  humiliation_or_coercion: '羞辱、威胁或强迫',
  known_urgent_risk_ended_without_safety_action: '已知紧迫风险下结束通话且未采取安全行动',
  false_confidentiality: '不准确的绝对保密承诺',
  fabricated_record: '工作记录与事实不符',
  encouraged_harm: '鼓励伤害行为',
  private_relationship: '建立不当私人关系',
}

/** 找不到对应中文时使用通用中文兜底，不向界面泄露接口内部值。 */
export const label = (map: Record<string, string>, value: string) => (
  Object.hasOwn(map, value) ? map[value] : '其他内容'
)

export function formatTime(value: string) {
  const at = new Date(value)
  return Number.isNaN(at.getTime()) ? value : at.toLocaleString('zh-CN', { hour12: false })
}
