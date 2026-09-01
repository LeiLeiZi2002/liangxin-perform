import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { createReport, getSession, putWorkRecord } from '../api/client'
import { workRecordInputSchema, type Turn, type WorkRecordInput } from '../api/contracts'
import {
  plannedActionLabels,
  referralDecisionLabels,
  riskLevelLabels,
  workRecordFieldLabels,
} from '../api/labels'

const initial: WorkRecordInput = {
  problem_understanding: '',
  risk_level: 'uncertain',
  risk_reasoning: '',
  risk_evidence_turn_ids: [],
  missing_information: [],
  planned_actions: [],
  referral_decision: 'not_needed',
  supervision_decision: false,
  follow_up: '',
  limitations: '',
}

const riskOptions = Object.entries(riskLevelLabels) as Array<
  [WorkRecordInput['risk_level'], string]
>
const actions = Object.entries(plannedActionLabels) as Array<
  [WorkRecordInput['planned_actions'][number], string]
>
const referralOptions = Object.entries(referralDecisionLabels) as Array<
  [WorkRecordInput['referral_decision'], string]
>

type EvidenceSegment = {
  key: string
  turnIds: string[]
  kind: 'opening' | 'exchange'
  workerText: string | null
  clientText: string
}

type RecordSceneCopy = {
  eyebrow: string
  title: string
  introduction: string
  observation: string | null
}

const neutralCopy: RecordSceneCopy = {
  eyebrow: 'PROFESSIONAL WORK RECORD',
  title: '专业工作记录',
  introduction: '请依据本次互动原话记录判断。没有查明的内容直接写明，不要根据经验补全。',
  observation: null,
}

function recordCopy(scene: string | null | undefined): RecordSceneCopy {
  if (scene === 'hotline') {
    return {
      eyebrow: 'HOTLINE WORK RECORD',
      title: '热线工作记录',
      introduction: '请依据本次热线互动的原话记录判断。没有查明的内容直接写明，不要根据经验补全。',
      observation: '如需记录语音线索，只记录本次实际听见的语速、停顿或声音变化；不要把系统生成的声音提示当成事实。',
    }
  }
  if (scene === 'online') {
    return {
      eyebrow: 'ONLINE SUPPORT RECORD',
      title: '在线咨询工作记录',
      introduction: '请依据本次文字互动的原话记录判断。没有查明的内容直接写明，不要根据经验补全。',
      observation: '可以记录文字回复节奏、连续短消息或明显停顿；这些只作为互动背景，不能单独用于推断心理状态。',
    }
  }
  return neutralCopy
}

type SavedRecordState = {
  locked: boolean
  snapshot: WorkRecordInput | null
}

class WorkRecordSubmissionError extends Error {
  readonly stage: 'work_record' | 'report_job'

  constructor(stage: 'work_record' | 'report_job') {
    super(stage)
    this.stage = stage
  }
}

function lines(value: string) {
  return value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

function readDraft(storageKey: string): WorkRecordInput {
  try {
    return { ...initial, ...JSON.parse(localStorage.getItem(storageKey) ?? '{}') }
  } catch {
    return initial
  }
}

function readSavedRecord(storageKey: string): SavedRecordState {
  const stored = localStorage.getItem(storageKey)
  if (stored === null) return { locked: false, snapshot: null }

  try {
    const parsed = workRecordInputSchema.safeParse(JSON.parse(stored))
    return { locked: true, snapshot: parsed.success ? parsed.data : null }
  } catch {
    return { locked: true, snapshot: null }
  }
}

function buildEvidenceSegments(transcript: Turn[]): EvidenceSegment[] {
  const grouped = new Map<string, Turn[]>()
  const turns = [...transcript].sort((left, right) => left.sequence - right.sequence)
  const firstWorkerSequence = turns.find((turn) => turn.speaker === 'worker')?.sequence
    ?? Number.POSITIVE_INFINITY

  for (const turn of turns) {
    const group = grouped.get(turn.client_turn_id) ?? []
    group.push(turn)
    grouped.set(turn.client_turn_id, group)
  }

  return [...grouped.entries()].flatMap<EvidenceSegment>(([key, group]) => {
    const workerTurns = group.filter((turn) => turn.speaker === 'worker')
    const clientTurns = group.filter((turn) => turn.speaker === 'client')
    if (
      workerTurns.length === 0
      && clientTurns.length > 0
      && clientTurns.every((turn) => turn.sequence < firstWorkerSequence)
    ) {
      return [{
        key,
        kind: 'opening' as const,
        turnIds: clientTurns.map((turn) => turn.id),
        workerText: null,
        clientText: clientTurns.map((turn) => turn.text).join('\n'),
      }]
    }
    if (workerTurns.length === 0 || clientTurns.length === 0) return []

    return [
      {
        key,
        kind: 'exchange' as const,
        turnIds: group.map((turn) => turn.id),
        workerText: workerTurns.map((turn) => turn.text).join('\n'),
        clientText: clientTurns.map((turn) => turn.text).join('\n'),
      },
    ]
  })
}

export function WorkRecordPage() {
  const { sessionId = '' } = useParams()
  const navigate = useNavigate()
  const storageKey = `work-record-draft:${sessionId}`
  const savedStorageKey = `work-record-saved:${sessionId}`
  const [savedRecord, setSavedRecord] = useState<SavedRecordState>(() =>
    readSavedRecord(savedStorageKey),
  )
  const [form, setForm] = useState<WorkRecordInput>(() =>
    savedRecord.snapshot ?? readDraft(storageKey),
  )
  const [error, setError] = useState('')
  const session = useQuery({
    queryKey: ['session', sessionId, 'work-record'],
    queryFn: () => getSession(sessionId),
    retry: false,
  })
  const evidenceSegments = useMemo(
    () => buildEvidenceSegments(session.data?.transcript ?? []),
    [session.data?.transcript],
  )
  const selectedEvidenceCount = evidenceSegments.filter((segment) =>
    segment.turnIds.every((turnId) => form.risk_evidence_turn_ids.includes(turnId)),
  ).length
  const copy = recordCopy(session.data?.session.scene)

  const save = useMutation({
    mutationFn: async (input: WorkRecordInput) => {
      if (localStorage.getItem(savedStorageKey) === null) {
        try {
          const saved = await putWorkRecord(sessionId, input)
          const confirmed = workRecordInputSchema.parse(saved)
          localStorage.setItem(savedStorageKey, JSON.stringify(confirmed))
          setForm(confirmed)
          setSavedRecord({ locked: true, snapshot: confirmed })
        } catch {
          throw new WorkRecordSubmissionError('work_record')
        }
      }
      try {
        return await createReport(sessionId)
      } catch {
        throw new WorkRecordSubmissionError('report_job')
      }
    },
    onSuccess: (job) => {
      localStorage.removeItem(storageKey)
      localStorage.removeItem(savedStorageKey)
      navigate(`/report-jobs/${job.id}`, { replace: true })
    },
    onError: (reason) => setError(
      reason instanceof WorkRecordSubmissionError && reason.stage === 'report_job'
        ? '工作记录已保存，但报告分析任务暂时未能创建。已填写内容仍保留，请再次提交。'
        : '工作记录暂时未能保存。已填写内容仍保留，请稍后再次提交。',
    ),
  })
  const isFormLocked = savedRecord.locked || save.isPending

  useEffect(() => {
    if (!isFormLocked) localStorage.setItem(storageKey, JSON.stringify(form))
  }, [form, isFormLocked, storageKey])

  function set<K extends keyof WorkRecordInput>(key: K, value: WorkRecordInput[K]) {
    if (isFormLocked) return
    setForm((current) => ({ ...current, [key]: value }))
  }

  function toggleEvidence(segment: EvidenceSegment) {
    if (isFormLocked) return
    const selectedIds = new Set(form.risk_evidence_turn_ids)
    const isSelected = segment.turnIds.every((turnId) => selectedIds.has(turnId))

    if (isSelected) {
      const segmentIds = new Set(segment.turnIds)
      set(
        'risk_evidence_turn_ids',
        form.risk_evidence_turn_ids.filter((turnId) => !segmentIds.has(turnId)),
      )
      return
    }

    const addedIds = segment.turnIds.filter((turnId) => !selectedIds.has(turnId))
    set('risk_evidence_turn_ids', [...form.risk_evidence_turn_ids, ...addedIds])
  }

  function submit() {
    if (savedRecord.locked) {
      setError('')
      if (!save.isPending) save.mutate(form)
      return
    }
    const parsed = workRecordInputSchema.safeParse(form)
    if (!parsed.success) {
      setError('请完整填写本次求助、安全研判、行动状态和判断限制。')
      return
    }
    setError('')
    if (!save.isPending) save.mutate(parsed.data)
  }

  return (
    <main className="archive-page record-page">
      <p className="eyebrow">{copy.eyebrow}</p>
      <h1>{copy.title}</h1>
      <p>{copy.introduction}</p>
      {copy.observation ? <p className="record-media-note">{copy.observation}</p> : null}

      {savedRecord.locked ? (
        <div className="record-saved-notice" role="status">
          工作记录已经保存，报告将使用这个版本。请重试生成报告。
        </div>
      ) : null}

      <div className="record-workspace">
        <section className="record-form-sheet" aria-labelledby="record-form-title">
          <header className="record-panel-heading">
            <span>01</span>
            <div>
              <h2 id="record-form-title">记录内容</h2>
              <p>记录结束时已经掌握的信息，并准确区分各项行动的当前状态。</p>
            </div>
          </header>

          <div className="record-form">
            <label>
              {workRecordFieldLabels.problem_understanding}
              <textarea
                aria-label={workRecordFieldLabels.problem_understanding}
                disabled={isFormLocked}
                value={form.problem_understanding}
                onChange={(event) => set('problem_understanding', event.target.value)}
              />
            </label>

            <fieldset>
              <legend id="risk-level-label">{workRecordFieldLabels.risk_level}</legend>
              <div className="record-option-list" role="radiogroup" aria-labelledby="risk-level-label">
                {riskOptions.map(([value, label]) => (
                  <label key={value}>
                    <input
                      type="radio"
                      name="risk"
                      value={value}
                      disabled={isFormLocked}
                      checked={form.risk_level === value}
                      onChange={() => set('risk_level', value)}
                    />
                    {label}
                  </label>
                ))}
              </div>
            </fieldset>

            <label>
              {workRecordFieldLabels.risk_reasoning}
              <textarea
                aria-label={workRecordFieldLabels.risk_reasoning}
                disabled={isFormLocked}
                value={form.risk_reasoning}
                onChange={(event) => set('risk_reasoning', event.target.value)}
              />
            </label>

            <label>
              {workRecordFieldLabels.missing_information}（每行一项）
              <textarea
                aria-label={workRecordFieldLabels.missing_information}
                disabled={isFormLocked}
                value={form.missing_information.join('\n')}
                onChange={(event) => set('missing_information', lines(event.target.value))}
              />
            </label>

            <fieldset>
              <legend>{workRecordFieldLabels.planned_actions}</legend>
              <p className="record-field-help">
                勾选本次已经讨论或采取过的工作类别；这不等同于相应行动已经执行。
              </p>
              <div className="record-option-list record-option-list--actions">
                {actions.map(([value, label]) => (
                  <label key={value}>
                    <input
                      type="checkbox"
                      disabled={isFormLocked}
                      checked={form.planned_actions.includes(value)}
                      onChange={(event) =>
                        set(
                          'planned_actions',
                          event.target.checked
                            ? [...form.planned_actions, value]
                            : form.planned_actions.filter((item) => item !== value),
                        )
                      }
                    />
                    {label}
                  </label>
                ))}
              </div>
            </fieldset>

            <label>
              {workRecordFieldLabels.referral_decision}
              <select
                aria-label={workRecordFieldLabels.referral_decision}
                disabled={isFormLocked}
                value={form.referral_decision}
                onChange={(event) =>
                  set('referral_decision', event.target.value as WorkRecordInput['referral_decision'])
                }
              >
                {referralOptions.map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </label>

            <label className="supervision-field">
              <input
                type="checkbox"
                disabled={isFormLocked}
                checked={form.supervision_decision}
                onChange={(event) => set('supervision_decision', event.target.checked)}
              />
              {workRecordFieldLabels.supervision_decision}
            </label>

            <label>
              {workRecordFieldLabels.follow_up}
              <textarea
                aria-label={workRecordFieldLabels.follow_up}
                disabled={isFormLocked}
                value={form.follow_up}
                onChange={(event) => set('follow_up', event.target.value)}
              />
            </label>
            <p className="record-field-help">
              请分别写明已经完成、已经同意、准备之后做的事项，以及下一步如何衔接。
            </p>

            <label>
              {workRecordFieldLabels.limitations}
              <textarea
                aria-label={workRecordFieldLabels.limitations}
                disabled={isFormLocked}
                value={form.limitations}
                onChange={(event) => set('limitations', event.target.value)}
              />
            </label>
          </div>
        </section>

        <aside className="record-evidence-panel" aria-labelledby="record-evidence-title">
          <header className="record-panel-heading">
            <span>02</span>
            <div>
              <h2 id="record-evidence-title">
                {workRecordFieldLabels.risk_evidence_turn_ids}
              </h2>
              <p>勾选能够支持关键判断或处置说明的原话片段。</p>
            </div>
          </header>

          {session.isPending ? <p className="evidence-state">正在整理会谈原话…</p> : null}
          {session.isError ? (
            <div className="evidence-state evidence-state--error">
              <strong>原话记录暂时无法读取</strong>
              <p>可以先填写其他内容，恢复后再回来选择证据。</p>
            </div>
          ) : null}
          {session.isSuccess && evidenceSegments.length === 0 ? (
            <p className="evidence-state">本次互动暂时没有可编组的原话片段。</p>
          ) : null}

          {evidenceSegments.length > 0 ? (
            <ol className="evidence-segment-list">
              {evidenceSegments.map((segment, index) => {
                const selected = segment.turnIds.every((turnId) =>
                  form.risk_evidence_turn_ids.includes(turnId),
                )
                const label = `原话片段 ${index + 1}`
                return (
                  <li key={segment.key} className={selected ? 'evidence-segment is-selected' : 'evidence-segment'}>
                    <label className="evidence-segment__toggle">
                      <input
                        type="checkbox"
                        disabled={isFormLocked}
                        checked={selected}
                        aria-label={`纳入关键判断与处置依据：${label}`}
                        onChange={() => toggleEvidence(segment)}
                      />
                      <span>{label}</span>
                      <small>{selected ? '已纳入' : '纳入判断依据'}</small>
                    </label>
                    <blockquote>
                      {segment.workerText ? (
                        <div>
                          <span>受测者</span>
                          <p>{segment.workerText}</p>
                        </div>
                      ) : null}
                      <div>
                        <span>{segment.kind === 'opening' ? '来访者主动开场' : '来访者'}</span>
                        <p>{segment.clientText}</p>
                      </div>
                    </blockquote>
                  </li>
                )
              })}
            </ol>
          ) : null}
        </aside>
      </div>

      <div className="record-submit-bar">
        <p>{`已选 ${selectedEvidenceCount} 个证据片段`}</p>
        <div>
          {error ? (
            <p className="record-error" role="alert">
              {error}
            </p>
          ) : null}
          <button
            className="button button--coral"
            type="button"
            disabled={save.isPending}
            onClick={submit}
          >
            {save.isPending
              ? (savedRecord.locked ? '正在生成报告…' : '正在保存…')
              : (savedRecord.locked ? '重试生成报告' : '提交工作记录')}
          </button>
        </div>
      </div>
    </main>
  )
}
