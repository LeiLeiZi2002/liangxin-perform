import { z } from 'zod'

export const sceneSchema = z.enum(['institution', 'hotline', 'online'])
export const caseTypeSchema = z.enum(['main', 'short'])
export const modelModeSchema = z.enum(['auto', 'live', 'fallback'])

export const demoConfigSchema = z.object({
  scene: sceneSchema,
  case_type: caseTypeSchema,
  task_count: z.number().int().min(1),
  soft_duration_minutes: z.number().int().min(1).nullable(),
  model_mode: modelModeSchema,
  require_work_record: z.boolean(),
})

export const providerConfigSchema = z.object({
  configured: z.boolean(),
  masked_key: z.string().nullable(),
  workspace_id: z.string().nullable(),
  report_model: z.string().min(1),
  actor_model: z.string().min(1),
  asr_model: z.string().min(1),
  tts_model: z.string().min(1),
  tts_voice: z.string().min(1),
  report_temperature: z.number().min(0).max(2),
  actor_temperature: z.number().min(0).max(2),
  actor_context_window_tokens: z.number().int().min(1),
  actor_max_output_tokens: z.number().int().min(1),
}).strict()

export const providerConfigUpdateSchema = providerConfigSchema
  .omit({ configured: true, masked_key: true })
  .extend({ api_key: z.string().max(512) })

export const providerCheckItemSchema = z.object({
  status: z.enum(['passed', 'failed']),
  message: z.string().nullable(),
})
export const providerCheckSchema = z.object({
  actor: providerCheckItemSchema,
  asr: providerCheckItemSchema,
  tts: providerCheckItemSchema,
}).strict()

export const runtimePhaseSchema = z.enum([
  'listening',
  'directing',
  'acting',
  'synthesizing',
  'playing',
  'technical_paused',
  'ended',
])

export const healthSchema = z.object({
  status: z.literal('ready'),
  service: z.literal('psych-assessment-demo'),
})

export const publicEntrySchema = z.object({
  role: z.string(),
  known_information: z.array(z.string()),
  task_boundary: z.array(z.string()),
})

export const caseMetadataSchema = z.object({
  case_id: z.string(), title: z.string(),
  case_type: caseTypeSchema, public_entry: publicEntrySchema, estimated_duration_minutes: z.number(),
  scene: sceneSchema.nullable(), media: z.enum(['voice', 'text']).nullable(),
  available_scenes: z.array(sceneSchema),
})
export const sessionSchema = z.object({
  id: z.string(), mode: z.enum(['assessment', 'experience']), scene: sceneSchema.nullable(),
  case_type: caseTypeSchema.nullable(), case_id: z.string(), media: z.enum(['voice', 'text']),
  status: z.enum(['active', 'ended']), model_mode: modelModeSchema,
  soft_duration_minutes: z.number().nullable(), created_at: z.string(), updated_at: z.string(),
  ended_at: z.string().nullable(), end_reason: z.enum(['user_ended', 'natural_closure', 'soft_time_reached', 'technical_interruption']).nullable(),
})
export const turnSchema = z.object({
  id: z.string(), sequence: z.number().int(), speaker: z.enum(['worker', 'client']), text: z.string(),
  client_turn_id: z.string(),
  provider: z.string().nullable(), degraded: z.boolean(), created_at: z.string(), audio_available: z.boolean(),
})
export const sessionDetailSchema = z.object({ session: sessionSchema, transcript: z.array(turnSchema) })

export const riskLevelSchema = z.enum(['no_identified', 'low', 'moderate', 'high', 'imminent', 'uncertain'])
export const plannedActionSchema = z.enum([
  'continue_assessment', 'stay_connected', 'contact_support', 'reduce_access',
  'supervisor', 'emergency_services', 'referral', 'follow_up',
  'emotion_stabilization', 'goal_clarification', 'conflict_deescalation',
  'autonomy_support', 'resource_linkage',
])
export const referralDecisionSchema = z.enum(['not_needed', 'consider', 'recommended', 'urgent'])
export const workRecordInputSchema = z.object({
  problem_understanding: z.string().trim().min(1).max(4000), risk_level: riskLevelSchema,
  risk_reasoning: z.string().trim().min(1).max(4000), risk_evidence_turn_ids: z.array(z.string().trim().min(1).max(500)).max(100),
  missing_information: z.array(z.string().trim().min(1).max(500)).max(50), planned_actions: z.array(plannedActionSchema).max(20),
  referral_decision: referralDecisionSchema, supervision_decision: z.boolean(),
  follow_up: z.string().trim().min(1).max(4000), limitations: z.string().trim().min(1).max(4000),
})
export const workRecordSchema = workRecordInputSchema.extend({ id: z.string(), session_id: z.string(), created_at: z.string(), updated_at: z.string() })

export const coreDimensionSchema = z.enum(['C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9'])
export const specialModuleSchema = z.enum(['S1a', 'S1b', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8'])
export const targetSchema = z.union([coreDimensionSchema, specialModuleSchema])
export const workRecordFieldSchema = z.enum([
  'problem_understanding', 'risk_level', 'risk_reasoning', 'risk_evidence_turn_ids',
  'missing_information', 'planned_actions', 'referral_decision', 'supervision_decision',
  'follow_up', 'limitations',
])

export const dialogueRefSchema = z.object({
  kind: z.literal('dialogue'), turn_id: z.string().min(1), quote: z.string().trim().min(1),
}).strict()
export const workRecordRefSchema = z.object({
  kind: z.literal('work_record'), field: workRecordFieldSchema, quote: z.string().trim().min(1),
}).strict()
export const audioEventRefSchema = z.object({
  kind: z.literal('audio_event'), event_id: z.string().min(1),
}).strict()
export const evidenceRefSchema = z.discriminatedUnion('kind', [
  dialogueRefSchema, workRecordRefSchema, audioEventRefSchema,
])

export const codedEvidenceSchema = z.object({
  unit_id: z.string().min(1), target: targetSchema, indicator_id: z.string().min(1),
  direction: z.enum(['support', 'limit', 'adverse']),
  strength: z.enum(['strong', 'moderate', 'weak']), context: z.string().min(1),
  alternative_reading: z.string().nullable(), ref: evidenceRefSchema,
}).strict()

export const bottomLineEventSchema = z.object({
  id: z.string().min(1),
  category: z.enum([
    'humiliation_or_coercion', 'known_urgent_risk_ended_without_safety_action',
    'false_confidentiality', 'fabricated_record', 'encouraged_harm', 'private_relationship',
  ]),
  detection: z.enum(['rule', 'semantic', 'rule_candidate_semantic_confirmed']),
  refs: z.array(evidenceRefSchema).min(1), description: z.string().min(1),
  reasoning: z.string().min(1), repair_observed: z.boolean().nullable().optional(),
}).strict()

export const materialConflictSchema = z.object({
  id: z.string().min(1), dialogue_ref: dialogueRefSchema.nullable(),
  work_record_ref: workRecordRefSchema.nullable(), description: z.string().min(1),
  affected_targets: z.array(targetSchema).min(1), impact: z.string().min(1),
}).strict().refine(
  (value) => value.dialogue_ref !== null || value.work_record_ref !== null,
  { message: '材料冲突必须引用对话或工作记录' },
)

export const unscoredReasonSchema = z.enum(['no_opportunity', 'insufficient_evidence', 'technical_failure'])
export const analysisOutcomeSchema = z.enum(['ok', 'analysis_failed'])
export const indicatorStatusSchema = z.enum([
  'demonstrated', 'partial', 'opportunity_missed', 'adverse',
  'no_opportunity', 'no_reliable_material',
])
export const levelCapReasonSchema = z.enum([
  'adverse_evidence', 'conditional_opportunity_unavailable', 'no_complex_opportunity',
])
export const evidenceConfidenceSchema = z.enum(['high', 'medium', 'low'])
export const opportunityOutcomeSchema = z.object({
  declared_target: targetSchema,
  kind: z.enum(['required', 'conditional']),
  fulfilled: z.boolean(),
  indicator_ids: z.array(z.string().min(1)),
  complex_opportunity: z.boolean(),
}).strict()

export const dimensionResultSchema = z.object({
  target: targetSchema, level: z.number().int().min(0).max(4).nullable(),
  unscored_reason: unscoredReasonSchema.nullable(), analysis_outcome: analysisOutcomeSchema,
  opportunities: z.array(opportunityOutcomeSchema),
  indicator_states: z.record(z.string(), indicatorStatusSchema), pattern: z.string(), rationale: z.string(),
  evidence: z.array(codedEvidenceSchema), counter_evidence: z.array(codedEvidenceSchema),
  representative_unit_ids: z.array(z.string()), limiting_unit_ids: z.array(z.string()),
  conditional_unavailable: z.array(z.string()), caps_applied: z.array(levelCapReasonSchema),
  evidence_confidence: evidenceConfidenceSchema.nullable(),
  evidence_confidence_factors: z.array(z.string()), next_level_gap: z.array(z.string()),
}).strict().superRefine((value, context) => {
  const reject = (message: string) => context.addIssue({ code: 'custom', message })
  const hasItems = (...items: unknown[][]) => items.some((item) => item.length > 0)

  if (value.analysis_outcome === 'analysis_failed') {
    if (value.level !== null || value.unscored_reason !== null) {
      reject('analysis_failed 时不得填写等级或未评分原因')
    }
    const hasConclusion = Object.keys(value.indicator_states).length > 0
      || value.pattern.length > 0
      || value.rationale.length > 0
      || hasItems(
        value.evidence,
        value.counter_evidence,
        value.representative_unit_ids,
        value.limiting_unit_ids,
        value.caps_applied,
        value.evidence_confidence_factors,
        value.next_level_gap,
      )
      || value.evidence_confidence !== null
    if (hasConclusion) reject('analysis_failed 时所有分析结论字段必须为空')
    return
  }

  if ((value.level === null) === (value.unscored_reason === null)) {
    reject('分析成功后等级与未评分原因必须且只能填写一项')
    return
  }

  if (value.unscored_reason !== null) {
    const hasLevelConclusion = value.pattern.length > 0
      || hasItems(
        value.representative_unit_ids,
        value.limiting_unit_ids,
        value.caps_applied,
        value.evidence_confidence_factors,
        value.next_level_gap,
      )
      || value.evidence_confidence !== null
    if (hasLevelConclusion) reject('未评分维度不得携带等级性结论')
    return
  }

  if (value.evidence_confidence === null) {
    reject('已评分维度必须填写证据置信程度')
  }
  if (
    Object.keys(value.indicator_states).length === 0
    || value.pattern.trim().length === 0
    || value.rationale.trim().length === 0
    || value.evidence.length === 0
    || value.representative_unit_ids.length === 0
  ) {
    reject('已评分维度不得缺少指标状态、结论、证据或代表单元')
  }
})

export const resultSummarySchema = z.object({
  scored_core_count: z.number().int().min(0).max(9),
  unscored: z.array(z.tuple([coreDimensionSchema, unscoredReasonSchema])),
  analysis_failed: z.array(targetSchema), activated_modules: z.array(specialModuleSchema),
  inactive_modules: z.array(z.tuple([specialModuleSchema, z.string()])),
  bottom_line_events: z.array(bottomLineEventSchema), screening_gap: z.boolean(),
  level_distribution: z.string().min(1), next_behaviors: z.array(z.string()),
}).strict()

export const reportJobStageSchema = z.enum([
  'queued', 'coding', 'scoring', 'assembling', 'succeeded', 'partial', 'failed',
])
export const reportJobSchema = z.object({
  id: z.string(), session_id: z.string(), stage: reportJobStageSchema,
  progress_percent: z.number().int().min(0).max(100), partial: z.boolean(),
  retryable: z.boolean(), report_id: z.string().nullable(),
  created_at: z.string(), updated_at: z.string(),
}).strict()

export const dimensionReportSchema = z.object({
  target: targetSchema, name: z.string(), level_anchor: z.string().nullable(), result: dimensionResultSchema,
}).strict()
export const reportSchema = z.object({
  id: z.string(), session_id: z.string(), job_id: z.string(), case_id: z.string(),
  scene: sceneSchema, media: z.enum(['voice', 'text']),
  summary: resultSummarySchema, dimensions: z.array(dimensionReportSchema),
  bottom_line_events: z.array(bottomLineEventSchema), material_conflicts: z.array(materialConflictSchema),
  screening_gap: z.boolean(), disclaimers: z.array(z.string()),
  rubric_fingerprint: z.string(), case_package_fingerprint: z.string(), model_fingerprint: z.string(),
  prompt_fingerprint: z.string(), input_fingerprint: z.string(),
  ai_draft_status: z.enum(['complete', 'partial']), created_at: z.string(),
}).strict()

export const rubricDocumentSchema = z.object({
  title: z.string().trim().min(1),
  markdown: z.string().trim().min(1),
}).strict()

export type Scene = z.infer<typeof sceneSchema>
export type CaseType = z.infer<typeof caseTypeSchema>
export type ModelMode = z.infer<typeof modelModeSchema>
export type DemoConfig = z.infer<typeof demoConfigSchema>
export type ProviderConfig = z.infer<typeof providerConfigSchema>
export type ProviderConfigUpdate = z.infer<typeof providerConfigUpdateSchema>
export type ProviderCheck = z.infer<typeof providerCheckSchema>
export type RuntimePhase = z.infer<typeof runtimePhaseSchema>
export type Health = z.infer<typeof healthSchema>
export type CaseMetadata = z.infer<typeof caseMetadataSchema>
export type Session = z.infer<typeof sessionSchema>
export type Turn = z.infer<typeof turnSchema>
export type WorkRecordInput = z.infer<typeof workRecordInputSchema>
export type WorkRecordRead = z.infer<typeof workRecordSchema>
export type WorkRecord = WorkRecordRead
export type CoreDimension = z.infer<typeof coreDimensionSchema>
export type SpecialModule = z.infer<typeof specialModuleSchema>
export type Target = z.infer<typeof targetSchema>
export type OpportunityOutcome = z.infer<typeof opportunityOutcomeSchema>
export type DimensionResult = z.infer<typeof dimensionResultSchema>
export type EvidenceRef = z.infer<typeof evidenceRefSchema>
export type CodedEvidence = z.infer<typeof codedEvidenceSchema>
export type BottomLineEvent = z.infer<typeof bottomLineEventSchema>
export type MaterialConflict = z.infer<typeof materialConflictSchema>
export type ResultSummary = z.infer<typeof resultSummarySchema>
export type ReportJob = z.infer<typeof reportJobSchema>
export type Report = z.infer<typeof reportSchema>
export type RubricDocument = z.infer<typeof rubricDocumentSchema>
