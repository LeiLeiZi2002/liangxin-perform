import { useMutation, useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronRight, FileCheck2, RotateCcw } from 'lucide-react'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { getReport, retryReportJob } from '../api/client'
import type { CodedEvidence, DimensionResult, EvidenceRef, Report, Target } from '../api/contracts'
import {
  bottomLineCategoryLabels,
  capReasonLabels,
  confidenceLabels,
  displayCaseName,
  displayIndicatorForMedia,
  displayTargetDescription,
  displayTargetNameForMedia,
  formatTime,
  indicatorNames,
  label as displayLabel,
  targetNames,
  unscoredReasonLabels,
} from '../api/labels'
import { EvidenceDrawer } from '../components/EvidenceDrawer'

type Dimension = Report['dimensions'][number]
type ReportMedia = Report['media']
type DisplayText = (value: string) => string

const sceneNames: Record<Report['scene'], string> = {
  institution: '机构面谈',
  hotline: '心理热线',
  online: '在线咨询',
}
const mediaNames: Record<ReportMedia, string> = { voice: '实时语音', text: '实时文字' }

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function createDisplayText(report: Report): DisplayText {
  const dimensionNames = new Map(report.dimensions.map((dimension) => [dimension.target, dimension.name]))
  const indicators = Object.keys(indicatorNames).sort((left, right) => right.length - left.length)
  const targets = (Object.keys(targetNames) as Target[]).sort((left, right) => right.length - left.length)

  return (value) => {
    let text = value
    if (report.case_id) text = text.replaceAll(report.case_id, displayCaseName(report.case_id))

    indicators.forEach((indicatorId) => {
      text = text.replace(
        new RegExp(`\\b${escapeRegExp(indicatorId)}\\b`, 'g'),
        displayIndicatorForMedia(indicatorId, report.media).name,
      )
    })
    targets.forEach((target) => {
      text = text.replace(
        new RegExp(`\\b${escapeRegExp(target)}\\b`, 'g'),
        dimensionNames.get(target) ?? displayTargetNameForMedia(target, report.media),
      )
    })
    text = text.replaceAll('未评分', '暂不形成等级')
    return text
  }
}

function EvidenceButton({
  evidence,
  onOpen,
  media,
}: {
  evidence: EvidenceRef
  onOpen: (ref: EvidenceRef) => void
  media: ReportMedia
}) {
  const isRiskEvidence = evidence.kind === 'work_record' && evidence.field === 'risk_evidence_turn_ids'
  const action = isRiskEvidence
    ? '查看风险判断原话'
    : evidence.kind === 'dialogue'
      ? media === 'text' ? '查看会谈原文' : '查看通话原文'
      : evidence.kind === 'work_record'
        ? '查看工作记录'
        : media === 'text' ? '查看媒介材料说明' : '查看声音材料说明'
  const quote = evidence.kind === 'audio_event' || isRiskEvidence ? null : evidence.quote
  const accessibleName = quote ? `${action}：${quote}` : action

  return (
    <button className="evidence-link" type="button" aria-label={accessibleName} onClick={() => onOpen(evidence)}>
      <span>{action}</span>
      {quote ? <span>“{quote}”</span> : null}
      <ChevronRight size={15} aria-hidden="true" />
    </button>
  )
}

function EvidenceList({
  title,
  items,
  empty,
  onOpen,
  displayText,
  media,
}: {
  title: string
  items: CodedEvidence[]
  empty: string
  onOpen: (ref: EvidenceRef) => void
  displayText: DisplayText
  media: ReportMedia
}) {
  return (
    <section className="dimension-evidence-group">
      <h4>{title}</h4>
      {items.length === 0 ? <p className="report-empty-note">{empty}</p> : (
        <ol>
          {items.map((item, index) => (
            <li key={index}>
              <EvidenceButton evidence={item.ref} onOpen={onOpen} media={media} />
              <p>{displayText(item.context)}</p>
              {item.alternative_reading
                ? <small>其他可能解释：{displayText(item.alternative_reading)}</small>
                : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function OpportunityFacts({ result, media, displayText }: {
  result: DimensionResult
  media: ReportMedia
  displayText: DisplayText
}) {
  return (
    <section className="dimension-opportunities">
      <div className="dimension-opportunities__heading">
        <h4>本次能够观察到的情境</h4>
      </div>

      {result.opportunities.length > 0 ? (
        <ol className="opportunity-list">
          {result.opportunities.map((opportunity, index) => (
            <li key={index}>
              <p>
                <span>{opportunity.kind === 'required' ? (media === 'text' ? '每次会谈都应观察' : '每次通话都应观察') : '仅在相应情境出现时观察'}</span>
                {' · '}
                <span>{opportunity.fulfilled ? '本次已出现相应情境' : '本次未出现相应情境'}</span>
                {opportunity.complex_opportunity ? <> · <span>包含较复杂情境</span></> : null}
              </p>
              {opportunity.indicator_ids.length > 0 ? (
                <div>
                  {opportunity.indicator_ids.map((indicatorId, indicatorIndex) => {
                    const indicator = displayIndicatorForMedia(indicatorId, media)
                    return (
                      <section key={indicatorIndex}>
                        <h5>{displayText(indicator.name)}</h5>
                        <p>{displayText(indicator.description)}</p>
                      </section>
                    )
                  })}
                </div>
              ) : <p className="report-empty-note">本次没有列出具体观察内容。</p>}
            </li>
          ))}
        </ol>
      ) : <p className="report-empty-note">本次没有记录可供核对的观察情境。</p>}
    </section>
  )
}

function ScoringDetails({
  result,
  onOpen,
  displayText,
  media,
}: {
  result: DimensionResult
  onOpen: (ref: EvidenceRef) => void
  displayText: DisplayText
  media: ReportMedia
}) {
  return (
    <details className="dimension-scoring-details">
      <summary>查看评分依据与观察范围</summary>
      <OpportunityFacts result={result} media={media} displayText={displayText} />

      <div className="dimension-evidence-grid">
        <EvidenceList
          title="完整支持证据"
          items={result.evidence}
          empty="本次未列出支持性引用。"
          onOpen={onOpen}
          displayText={displayText}
          media={media}
        />
        <EvidenceList
          title="完整限制证据"
          items={result.counter_evidence}
          empty="本次未记录限制性引用。"
          onOpen={onOpen}
          displayText={displayText}
          media={media}
        />
      </div>

      <div className="dimension-detail-grid">
        <section className="dimension-detail-block">
          <h4>材料对判断的支持程度</h4>
          {result.evidence_confidence
            ? <strong>{displayLabel(confidenceLabels, result.evidence_confidence)}</strong>
            : <p>本次未形成材料支持程度说明。</p>}
          {result.evidence_confidence_factors.length > 0 ? (
            <ul>{result.evidence_confidence_factors.map((item, index) => (
              <li key={index}>{displayText(item)}</li>
            ))}</ul>
          ) : null}
        </section>

        {result.caps_applied.length > 0 ? (
          <section className="dimension-detail-block dimension-detail-block--cap">
            <h4>影响本次等级判断的材料条件</h4>
            <ul>{result.caps_applied.map((item, index) => (
              <li key={index}>{displayLabel(capReasonLabels, item)}</li>
            ))}</ul>
          </section>
        ) : null}

        {result.conditional_unavailable.length > 0 ? (
          <section className="conditional-unavailable">
            <h4>本次没有出现的条件情境</h4>
            <ul>{result.conditional_unavailable.map((item, index) => (
              <li key={index}>{displayText(item)}</li>
            ))}</ul>
          </section>
        ) : null}
      </div>
    </details>
  )
}

function AbilityDescription({
  result,
  displayText,
  media,
}: {
  result: DimensionResult
  displayText: DisplayText
  media: ReportMedia
}) {
  return (
    <section className="dimension-description">
      <h4>这项能力主要观察什么</h4>
      <p>{displayText(displayTargetDescription(result.target, media))}</p>
    </section>
  )
}

function DimensionCard({
  dimension,
  domId,
  onOpen,
  displayText,
  media,
}: {
  dimension: Dimension
  domId: string
  onOpen: (ref: EvidenceRef) => void
  displayText: DisplayText
  media: ReportMedia
}) {
  const result = dimension.result

  if (result.analysis_outcome === 'analysis_failed') {
    return (
      <article id={domId} className="report-dimension report-dimension--failed" aria-label={dimension.name}>
        <header><h3>{dimension.name}</h3></header>
        <AbilityDescription result={result} displayText={displayText} media={media} />
        <div className="dimension-state-callout">
          <strong>这项能力暂未完成分析</strong>
          <p>原因来自分析过程，不能据此判断受测者的能力或材料质量。</p>
        </div>
        <ScoringDetails result={result} onOpen={onOpen} displayText={displayText} media={media} />
      </article>
    )
  }

  if (result.unscored_reason) {
    return (
      <article id={domId} className="report-dimension report-dimension--unscored" aria-label={dimension.name}>
        <header><h3>{dimension.name}</h3></header>
        <AbilityDescription result={result} displayText={displayText} media={media} />
        <div className="dimension-state-callout">
          <strong>本次暂不形成等级</strong>
          <p>原因：{displayLabel(unscoredReasonLabels, result.unscored_reason)}</p>
          {result.rationale ? <p>{displayText(result.rationale)}</p> : null}
          <p>这不表示受测者不具备该项能力。</p>
        </div>
        <ScoringDetails result={result} onOpen={onOpen} displayText={displayText} media={media} />
      </article>
    )
  }

  const representative = result.evidence
    .filter((item) => result.representative_unit_ids.includes(item.unit_id))
    .slice(0, 2)

  return (
    <article id={domId} className="report-dimension" aria-label={dimension.name}>
      <header><h3>{dimension.name}</h3></header>
      <AbilityDescription result={result} displayText={displayText} media={media} />

      <strong className="dimension-level">
        {result.level === null ? '本次未形成等级描述' : `本次形成 ${result.level} 级描述`}
      </strong>

      <section className="level-anchor">
        <h4>这一等级表示</h4>
        <p>{dimension.level_anchor ? displayText(dimension.level_anchor) : '本次未提供对应的等级描述。'}</p>
      </section>

      <section className="dimension-narrative">
        <h4>本次观察到的表现</h4>
        <p>{displayText(result.pattern)}</p>
      </section>

      <section className="dimension-narrative">
        <h4>为什么这样判断</h4>
        <p>{displayText(result.rationale)}</p>
      </section>

      <section className="dimension-detail-block">
        <h4>要进一步判断，还需要看到</h4>
        {result.next_level_gap.length > 0 ? (
          <ul>{result.next_level_gap.map((item, index) => <li key={index}>{displayText(item)}</li>)}</ul>
        ) : <p>现有材料未形成需要补充观察的内容。</p>}
      </section>

      <section className="dimension-representative-evidence">
        <h4>代表性原话</h4>
        {representative.length > 0 ? (
          <div>{representative.map((item, index) => (
            <EvidenceButton key={index} evidence={item.ref} onOpen={onOpen} media={media} />
          ))}</div>
        ) : <p className="report-empty-note">本次未列出代表性原话。</p>}
      </section>

      <ScoringDetails result={result} onOpen={onOpen} displayText={displayText} media={media} />
    </article>
  )
}

export function ReportPage() {
  const { reportId = '' } = useParams()
  const navigate = useNavigate()
  const report = useQuery({ queryKey: ['report', reportId], queryFn: () => getReport(reportId), retry: false })
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceRef | null>(null)
  const retry = useMutation({
    mutationFn: (jobId: string) => retryReportJob(jobId),
    onSuccess: (job) => navigate(`/report-jobs/${job.id}`, { replace: true }),
  })

  if (report.isPending) {
    return <article className="loading-sheet"><p>正在读取证据报告…</p></article>
  }
  if (report.isError || !report.data) {
    return (
      <article className="error-sheet">
        <h1>报告暂时无法读取</h1>
        <p>请检查本地服务连接后重试。</p>
        <button className="button button--ink" type="button" onClick={() => void report.refetch()}>
          <RotateCcw size={16} aria-hidden="true" />重新读取
        </button>
      </article>
    )
  }

  const data = report.data
  const displayText = createDisplayText(data)
  const dimensionNames = new Map(data.dimensions.map((dimension) => [dimension.target, dimension.name]))
  const displayDimensionName = (target: Target) => (
    dimensionNames.get(target) ?? displayTargetNameForMedia(target, data.media)
  )
  const dimensions = data.dimensions.map((dimension, index) => ({ dimension, domId: `dimension-${index + 1}` }))
  const coreDimensions = dimensions.filter(({ dimension }) => dimension.target.startsWith('C'))
  const modules = dimensions.filter(({ dimension }) => dimension.target.startsWith('S'))
  const coreResults = coreDimensions.map(({ dimension }) => dimension.result)
  const scoredCount = coreResults.filter((result) => (
    result.analysis_outcome !== 'analysis_failed' && !result.unscored_reason && result.level !== null
  )).length
  const unscoredCount = coreResults.filter((result) => (
    result.analysis_outcome !== 'analysis_failed' && Boolean(result.unscored_reason)
  )).length
  const failedCount = coreResults.filter((result) => result.analysis_outcome === 'analysis_failed').length
  const hasPriorityFindings = data.bottom_line_events.length > 0 || data.material_conflicts.length > 0 || data.screening_gap

  return (
    <article className="archive-page report-page page-enter">
      <header className="report-cover">
        <div>
          <p className="archive-kicker">管理者查看 · 待核对分析稿</p>
          <h1>初阶心理服务从业者胜任力测评报告</h1>
          <p>案例：{displayCaseName(data.case_id)}</p>
          <p>场域：{sceneNames[data.scene]} · {mediaNames[data.media]}</p>
          <p>生成时间：{formatTime(data.created_at)}</p>
        </div>
        <FileCheck2 size={38} strokeWidth={1.2} aria-hidden="true" />
      </header>

      <aside className="ai-review-notice" aria-label="报告核对提示">
        <AlertTriangle size={23} aria-hidden="true" />
        <div><strong>本报告由大模型依据冻结的会谈原文和工作记录生成，正式使用前必须逐项核对原始材料。</strong><p>仅用于发展性反馈。</p></div>
      </aside>

      {data.ai_draft_status === 'partial' ? (
        <section className="partial-report-notice">
          <div>
            <strong>部分维度分析尚未完成</strong>
            <p>现有结论已经保留；未完成维度不会被写成材料不足或暂不形成等级。</p>
          </div>
          <button className="button button--ink" type="button" disabled={retry.isPending} onClick={() => retry.mutate(data.job_id)}>
            <RotateCcw size={16} aria-hidden="true" />
            {retry.isPending ? '正在重新发起…' : '重新分析未完成维度'}
          </button>
          {retry.isError ? <p className="record-error" role="alert">重新分析暂未发起，请稍后再试。</p> : null}
        </section>
      ) : null}

      <section className="report-summary" aria-labelledby="report-summary-title">
        <header><p>01 / 结果概览</p><h2 id="report-summary-title">本次结果概览</h2></header>
        <p>以下数量仅统计九项核心能力，专项能力另见后文。</p>
        <dl className="summary-lead">
          <div><dt>已形成等级数</dt><dd>{scoredCount} 项</dd></div>
          <div><dt>暂不形成等级数</dt><dd>{unscoredCount} 项</dd></div>
          <div><dt>分析未完成数</dt><dd>{failedCount} 项</dd></div>
        </dl>
        <p>{displayText(data.summary.level_distribution)}</p>

        <nav aria-label="能力结果导航">
          <ul>{dimensions.map(({ dimension, domId }) => (
            <li key={domId}><a href={`#${domId}`}>{dimension.name}</a></li>
          ))}</ul>
        </nav>
      </section>

      <section className="report-section report-findings" aria-labelledby="priority-findings-title">
        <header><p>02 / 核对事项</p><h2 id="priority-findings-title">需要优先核对的事项</h2></header>

        {!hasPriorityFindings ? (
          <p>本次没有需要特别核对的异常事项，基础风险筛查已经完成。</p>
        ) : (
          <>
            {data.bottom_line_events.length > 0 ? (
              <section className="finding-block finding-block--critical">
                <h3>需要立即核对的严重问题</h3>
                {data.bottom_line_events.map((event) => (
                  <article key={event.id}>
                    <strong>{displayLabel(bottomLineCategoryLabels, event.category)}</strong>
                    <p>{displayText(event.description)}</p>
                    <p>{displayText(event.reasoning)}</p>
                    <div className="finding-refs">{event.refs.map((ref, index) => (
                      <EvidenceButton key={index} evidence={ref} onOpen={setSelectedEvidence} media={data.media} />
                    ))}</div>
                  </article>
                ))}
              </section>
            ) : null}

            <section className="finding-block">
              <h3>基础风险筛查</h3>
              <strong>{data.screening_gap ? '本次未完成基础风险筛查' : '本次已完成基础风险筛查'}</strong>
            </section>

            {data.material_conflicts.length > 0 ? (
              <section className="finding-block">
                <h3>对话与工作记录不一致之处</h3>
                {data.material_conflicts.map((conflict) => (
                  <article key={conflict.id}>
                    <p>{displayText(conflict.description)}</p>
                    <strong>影响：{displayText(conflict.impact)}</strong>
                    <p>涉及能力：{conflict.affected_targets.map(displayDimensionName).join('、')}</p>
                    <div className="finding-refs">
                      {conflict.dialogue_ref
                        ? <EvidenceButton evidence={conflict.dialogue_ref} onOpen={setSelectedEvidence} media={data.media} />
                        : null}
                      {conflict.work_record_ref
                        ? <EvidenceButton evidence={conflict.work_record_ref} onOpen={setSelectedEvidence} media={data.media} />
                        : null}
                    </div>
                  </article>
                ))}
              </section>
            ) : null}
          </>
        )}
      </section>

      <section className="report-section" aria-labelledby="core-abilities-title">
        <header><p>03 / 核心能力</p><h2 id="core-abilities-title">核心能力</h2></header>
        <div className="report-dimension-list">
          {coreDimensions.length > 0
            ? coreDimensions.map(({ dimension, domId }) => (
              <DimensionCard
                key={domId}
                dimension={dimension}
                domId={domId}
                onOpen={setSelectedEvidence}
                displayText={displayText}
                media={data.media}
              />
            ))
            : <p className="report-empty-note">报告中没有核心能力结果。</p>}
        </div>
      </section>

      <section className="report-section" aria-labelledby="special-abilities-title">
        <header><p>04 / 专项能力</p><h2 id="special-abilities-title">专项能力</h2></header>
        <div className="report-dimension-list">
          {modules.length > 0
            ? modules.map(({ dimension, domId }) => (
              <DimensionCard
                key={domId}
                dimension={dimension}
                domId={domId}
                onOpen={setSelectedEvidence}
                displayText={displayText}
                media={data.media}
              />
            ))
            : <p className="report-empty-note">本次没有启用专项能力评估。</p>}
        </div>
        {data.summary.inactive_modules.length > 0 ? (
          <details className="inactive-modules">
            <summary>本次未启用的专项评估（{data.summary.inactive_modules.length}项）</summary>
            <ul>{data.summary.inactive_modules.map(([target, reason], index) => (
              <li key={index}>
                <strong>{displayDimensionName(target)}</strong>
                <span>{displayText(reason)}</span>
              </li>
            ))}</ul>
          </details>
        ) : null}
      </section>

      <section className="report-disclaimers" aria-labelledby="report-usage-title">
        <p>05 / 使用说明</p><h2 id="report-usage-title">报告如何使用</h2>
        <ol>{data.disclaimers.map((item, index) => <li key={index}>{displayText(item)}</li>)}</ol>
      </section>

      <details className="report-provenance">
        <summary>技术复核记录</summary>
        <dl>
          <div><dt>量规校验码</dt><dd>{data.rubric_fingerprint}</dd></div>
          <div><dt>案例材料校验码</dt><dd>{data.case_package_fingerprint}</dd></div>
          <div><dt>分析模型校验码</dt><dd>{data.model_fingerprint}</dd></div>
          <div><dt>分析提示校验码</dt><dd>{data.prompt_fingerprint}</dd></div>
          <div><dt>输入材料校验码</dt><dd>{data.input_fingerprint}</dd></div>
        </dl>
      </details>

      {selectedEvidence ? (
        <EvidenceDrawer sessionId={data.session_id} evidence={selectedEvidence} media={data.media} onClose={() => setSelectedEvidence(null)} />
      ) : null}
    </article>
  )
}
