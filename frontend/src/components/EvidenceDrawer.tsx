import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { getSession, getWorkRecord } from '../api/client'
import type { EvidenceRef, Turn, WorkRecordRead } from '../api/contracts'
import {
  label,
  plannedActionLabels,
  referralDecisionLabels,
  riskLevelLabels,
  workRecordFieldLabels,
} from '../api/labels'

interface EvidenceDrawerProps {
  sessionId: string
  evidence: EvidenceRef
  media?: 'voice' | 'text'
  onClose: () => void
}

type WorkRecordEvidence = Extract<EvidenceRef, { kind: 'work_record' }>
type WorkRecordDisplayItem = {
  raw: string
  display: string
}

function savedQuoteText(evidence: WorkRecordEvidence, media: 'voice' | 'text') {
  return evidence.field === 'risk_evidence_turn_ids'
    ? media === 'text' ? '引用的会谈原文' : '引用的通话原文'
    : evidence.quote
}

function displayItems(
  record: WorkRecordRead,
  field: WorkRecordEvidence['field'],
): WorkRecordDisplayItem[] {
  const value = record[field]
  if (field === 'planned_actions') {
    return record.planned_actions.map((item) => ({
      raw: item,
      display: label(plannedActionLabels, item),
    }))
  }
  if (field === 'risk_level') {
    return [{ raw: record.risk_level, display: label(riskLevelLabels, record.risk_level) }]
  }
  if (field === 'referral_decision') {
    return [{
      raw: record.referral_decision,
      display: label(referralDecisionLabels, record.referral_decision),
    }]
  }
  if (field === 'supervision_decision') {
    const display = record.supervision_decision ? '是' : '否'
    return [{ raw: display, display }]
  }
  if (Array.isArray(value)) {
    return value.map((item) => ({ raw: item, display: item }))
  }
  const text = String(value)
  return [{ raw: text, display: text }]
}

function containsQuote(item: WorkRecordDisplayItem, quote: string) {
  return item.raw.includes(quote) || item.display.includes(quote)
}

function highlightedValue(item: WorkRecordDisplayItem, quote: string): ReactNode {
  const index = item.display.indexOf(quote)
  if (index < 0) return containsQuote(item, quote) ? <mark>{item.display}</mark> : item.display
  return (
    <>
      {item.display.slice(0, index)}
      <mark>{quote}</mark>
      {item.display.slice(index + quote.length)}
    </>
  )
}

function SavedQuoteFallback({ message, quote, alert = false }: {
  message: string
  quote: string
  alert?: boolean
}) {
  return (
    <div className="evidence-drawer__fallback" role={alert ? 'alert' : undefined}>
      <p>{message}</p>
      <blockquote>{quote}</blockquote>
    </div>
  )
}

function RiskEvidenceContent({
  record,
  selectedTurnId,
  turns,
  turnsPending,
  turnsError,
  media,
}: {
  record: WorkRecordRead
  selectedTurnId: string
  turns: Turn[]
  turnsPending: boolean
  turnsError: boolean
  media: 'voice' | 'text'
}) {
  if (record.risk_evidence_turn_ids.length === 0) {
    return <p>这部分没有记录具体内容。</p>
  }
  if (turnsPending) {
    return <p>正在核对本次{media === 'text' ? '会谈' : '通话'}原文…</p>
  }
  if (turnsError) {
    return (
      <div className="evidence-drawer__fallback" role="alert">
        <p>本次{media === 'text' ? '会谈' : '通话'}记录暂时无法读取，工作记录中的风险判断原话暂时无法核对。</p>
      </div>
    )
  }

  const turnsById = new Map(turns.map((turn) => [turn.id, turn]))

  return (
    <>
      <ol className="evidence-context-list">
        {record.risk_evidence_turn_ids.map((turnId, index) => {
          const turn = turnsById.get(turnId)
          return (
            <li
              key={`${turnId}-${index}`}
              aria-current={turnId === selectedTurnId ? 'true' : undefined}
              className={turnId === selectedTurnId ? 'is-target' : undefined}
            >
              <span>
                引用的{media === 'text' ? '会谈' : '通话'}原文 {index + 1}
                {turn ? ` · ${turn.speaker === 'worker' ? (media === 'text' ? '受测者' : '接线人员') : (media === 'text' ? '来访者' : '来电者')}` : ''}
              </span>
              {turn ? <blockquote>{turn.text}</blockquote> : <p>未在本次{media === 'text' ? '会谈' : '通话'}记录中找到对应原文</p>}
            </li>
          )
        })}
      </ol>
      <p>引用位置取自生成报告时冻结保存的工作记录，逐字原话取自本次{media === 'text' ? '会谈' : '通话'}记录。</p>
    </>
  )
}

function WorkRecordEvidenceContent({
  record,
  evidence,
  turns,
  turnsPending,
  turnsError,
  media,
}: {
  record: WorkRecordRead
  evidence: WorkRecordEvidence
  turns: Turn[]
  turnsPending: boolean
  turnsError: boolean
  media: 'voice' | 'text'
}) {
  if (evidence.field === 'risk_evidence_turn_ids') {
    return (
      <RiskEvidenceContent
        record={record}
        selectedTurnId={evidence.quote}
        turns={turns}
        turnsPending={turnsPending}
        turnsError={turnsError}
        media={media}
      />
    )
  }

  const items = displayItems(record, evidence.field)
  const quoteFound = items.some((item) => containsQuote(item, evidence.quote))
  const isList = Array.isArray(record[evidence.field])

  return (
    <>
      {isList ? (
        items.length > 0 ? (
          <ul>
            {items.map((item, index) => (
              <li key={`${item.raw}-${index}`}>{highlightedValue(item, evidence.quote)}</li>
            ))}
          </ul>
        ) : <p>这部分没有记录具体内容。</p>
      ) : (
        <blockquote>{highlightedValue(items[0], evidence.quote)}</blockquote>
      )}
      {!quoteFound ? (
        <SavedQuoteFallback
          message="当前工作记录中未找到这段引用，报告保存的原话如下。"
          quote={savedQuoteText(evidence, media)}
        />
      ) : null}
      <p>以上内容取自生成报告时冻结保存的工作记录。</p>
    </>
  )
}

export function EvidenceDrawer({ sessionId, evidence, media = 'voice', onClose }: EvidenceDrawerProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const dialogue = evidence.kind === 'dialogue'
  const riskEvidence = evidence.kind === 'work_record'
    && evidence.field === 'risk_evidence_turn_ids'
  const detail = useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => getSession(sessionId),
    enabled: dialogue || riskEvidence,
    retry: false,
  })
  const workRecord = useQuery({
    queryKey: ['work-record', sessionId],
    queryFn: () => getWorkRecord(sessionId),
    enabled: evidence.kind === 'work_record',
    retry: false,
  })
  const turns = [...(detail.data?.transcript ?? [])].sort((left, right) => left.sequence - right.sequence)
  const targetIndex = dialogue ? turns.findIndex((turn) => turn.id === evidence.turn_id) : -1
  const context = targetIndex >= 0
    ? turns.slice(Math.max(0, targetIndex - 1), targetIndex + 2)
    : []

  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null
    closeButtonRef.current?.focus()
    return () => trigger?.focus()
  }, [])

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab') return
    event.preventDefault()
    closeButtonRef.current?.focus()
  }

  return createPortal(
    <div className="evidence-drawer" role="dialog" aria-modal="true" aria-label="查看原始材料" onKeyDown={handleKeyDown}>
      <div className="evidence-drawer__sheet">
        <header>
          <div>
            <p className="archive-kicker">原始材料</p>
            <h2>查看原始材料</h2>
          </div>
          <button ref={closeButtonRef} type="button" onClick={onClose} aria-label="关闭原始材料">
            <X size={18} aria-hidden="true" />关闭
          </button>
        </header>

        {evidence.kind === 'dialogue' ? (
          <section aria-label="对话原文">
            <p className="evidence-drawer__source">{media === 'text' ? '会谈原文' : '通话转写'} · 引用内容及前后语境</p>
            {detail.isPending ? <p>正在读取{media === 'text' ? '会谈' : '通话'}上下文…</p> : null}
            {detail.isError ? (
              <div className="evidence-drawer__fallback" role="alert">
                <p>{media === 'text' ? '会谈' : '通话'}记录暂时无法读取，报告保存的原话如下。</p>
                <blockquote>{evidence.quote}</blockquote>
              </div>
            ) : null}
            {!detail.isPending && !detail.isError && targetIndex < 0 ? (
              <div className="evidence-drawer__fallback">
                <p>当前{media === 'text' ? '会谈' : '通话'}记录中没有找到这段引用，报告保存的原话如下。</p>
                <blockquote>{evidence.quote}</blockquote>
              </div>
            ) : null}
            {context.length > 0 ? (
              <ol className="evidence-context-list">
                {context.map((turn) => (
                  <li
                    key={turn.id}
                    aria-current={turn.id === evidence.turn_id ? 'true' : undefined}
                    className={turn.id === evidence.turn_id ? 'is-target' : undefined}
                  >
                    <span>{turn.speaker === 'worker' ? (media === 'text' ? '受测者' : '接线人员') : (media === 'text' ? '来访者' : '来电者')}</span>
                    <p>{turn.text}</p>
                  </li>
                ))}
              </ol>
            ) : null}
          </section>
        ) : null}

        {evidence.kind === 'work_record' ? (
          <section aria-label="工作记录原文" className="work-record-evidence">
            <p className="evidence-drawer__source">工作记录原文</p>
            <h3>{workRecordFieldLabels[evidence.field]}</h3>
            {workRecord.isPending ? <p>正在读取工作记录原文…</p> : null}
            {workRecord.isError ? (
              <SavedQuoteFallback
                alert
                message="工作记录暂时无法读取，报告保存的原话如下。"
                quote={savedQuoteText(evidence, media)}
              />
            ) : null}
            {workRecord.data ? (
              <WorkRecordEvidenceContent
                record={workRecord.data}
                evidence={evidence}
                turns={turns}
                turnsPending={detail.isPending}
                turnsError={detail.isError}
                media={media}
              />
            ) : null}
          </section>
        ) : null}

        {evidence.kind === 'audio_event' ? (
          <section aria-label={media === 'text' ? '媒介材料说明' : '声音材料说明'} className="audio-evidence-unavailable">
            <p className="evidence-drawer__source">{media === 'text' ? '媒介材料说明' : '声音材料说明'}</p>
            <h3>{media === 'text' ? '本次没有独立媒介事件材料' : '本次未分析声音表现'}</h3>
            <p>{media === 'text' ? '本次判断依据会谈原文和工作记录。' : '本次判断只依据通话转写和工作记录，未分析语速、停顿、语调等声音表现。'}</p>
          </section>
        ) : null}
      </div>
    </div>,
    document.body,
  )
}
