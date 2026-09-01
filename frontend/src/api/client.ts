import type { ZodType } from 'zod'

import { caseMetadataSchema, demoConfigSchema, healthSchema, providerCheckSchema, providerConfigSchema, reportJobSchema, reportSchema, rubricDocumentSchema, sessionDetailSchema, sessionSchema, workRecordSchema, type CaseType, type DemoConfig, type Health, type ProviderConfig, type ProviderConfigUpdate, type RubricDocument, type Scene, type WorkRecordInput } from './contracts'
import { z } from 'zod'

export type ApiErrorKind = 'network' | 'validation' | 'provider' | 'protocol'

export class ApiError extends Error {
  public readonly kind: ApiErrorKind
  public readonly status: number | null
  public readonly details: unknown

  constructor(
    message: string,
    kind: ApiErrorKind,
    status: number | null = null,
    details: unknown = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.details = details
  }
}

const configuredApiBase = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const apiBase = configuredApiBase.replace(/\/api$/, '')

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'object' && payload !== null && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const firstMessage = detail.find(
        (item): item is { msg: string } =>
          typeof item === 'object' && item !== null && 'msg' in item && typeof item.msg === 'string',
      )
      if (firstMessage) return firstMessage.msg
    }
  }
  return fallback
}

async function readPayload(response: Response): Promise<unknown> {
  if (!(response.headers.get('content-type') ?? '').includes('application/json')) return null
  try {
    return await response.json()
  } catch {
    return null
  }
}

export async function apiRequest<T>(
  path: string,
  schema: ZodType<T>,
  init?: RequestInit,
): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${apiBase}${path}`, init)
  } catch (cause) {
    throw new ApiError('无法连接本地服务，请确认后端已经启动。', 'network', null, cause)
  }

  const payload = await readPayload(response)
  if (!response.ok) {
    const isClientError = response.status >= 400 && response.status < 500
    throw new ApiError(
      errorMessage(payload, isClientError ? '提交内容未通过校验。' : '服务暂时不可用。'),
      isClientError ? 'validation' : 'provider',
      response.status,
      payload,
    )
  }

  const parsed = schema.safeParse(payload)
  if (!parsed.success) {
    throw new ApiError('服务返回的数据格式不符合约定。', 'protocol', response.status, parsed.error)
  }
  return parsed.data
}

export function getDemoConfig(): Promise<DemoConfig> {
  return apiRequest('/api/demo-config', demoConfigSchema)
}

export function updateDemoConfig(config: DemoConfig): Promise<DemoConfig> {
  return apiRequest('/api/demo-config', demoConfigSchema, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
}

export function getProviderConfig(): Promise<ProviderConfig> {
  return apiRequest('/api/provider-config', providerConfigSchema)
}

export function updateProviderConfig(config: ProviderConfigUpdate): Promise<ProviderConfig> {
  return apiRequest('/api/provider-config', providerConfigSchema, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
}

export function checkProviderReadiness(requiresSpeech: boolean) {
  return apiRequest('/api/provider-config/check', providerCheckSchema, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requires_speech: requiresSpeech }),
  })
}

export function getHealth(): Promise<Health> {
  return apiRequest('/api/health', healthSchema)
}

export const getSession = (id: string) => apiRequest(`/api/sessions/${id}`, sessionDetailSchema)
export const listCases = (scene: Scene, caseType: CaseType) => apiRequest(`/api/cases?scene=${scene}&case_type=${caseType}`, z.array(caseMetadataSchema))
export const drawCase = (scene: Scene, caseType: CaseType) => apiRequest('/api/cases/draw', caseMetadataSchema, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scene, case_type: caseType, excluded_case_ids: [] }) })
export const createSession = (input: { mode: 'assessment' | 'experience'; scene: Scene; case_type: CaseType; case_id: string }) => apiRequest('/api/sessions', sessionSchema, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
export const endSession = (
  id: string,
  reason: 'user_ended' | 'technical_interruption' = 'user_ended',
) => apiRequest(`/api/sessions/${id}/end`, sessionSchema, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ reason }),
})
export const putWorkRecord = (id: string, input: WorkRecordInput) => apiRequest(`/api/sessions/${id}/work-record`, workRecordSchema, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) })
export const getWorkRecord = (id: string) => apiRequest(`/api/sessions/${id}/work-record`, workRecordSchema)
export const createReport = (sessionId: string) => apiRequest(`/api/sessions/${sessionId}/reports`, reportJobSchema, { method: 'POST' })
export const getReportJob = (jobId: string) => apiRequest(`/api/report-jobs/${jobId}`, reportJobSchema)
export const retryReportJob = (jobId: string) => apiRequest(`/api/report-jobs/${jobId}/retry`, reportJobSchema, { method: 'POST' })
export const getReport = (reportId: string) => apiRequest(`/api/reports/${reportId}`, reportSchema)
export const getRubricDocument = (): Promise<RubricDocument> => apiRequest('/api/rubric', rubricDocumentSchema)
