import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileSearch, RotateCcw } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { getReportJob, retryReportJob } from '../api/client'
import type { ReportJob } from '../api/contracts'

const stageCopy: Record<ReportJob['stage'], { title: string; detail: string }> = {
  queued: {
    title: '正在准备分析材料',
    detail: '工作记录已保存，正在核对本次通话与个案材料。',
  },
  coding: {
    title: '正在整理通话与工作记录',
    detail: '系统正在建立可回查的证据引用，并检查相反材料。',
  },
  scoring: {
    title: '正在逐项对照能力量规',
    detail: '各核心能力和已启用专项模块正在分组分析。',
  },
  assembling: {
    title: '正在组装证据报告',
    detail: '系统正在汇总等级锚点、限制说明和版本依据。',
  },
  succeeded: { title: '分析报告已生成', detail: '正在打开报告。' },
  partial: { title: '报告已形成', detail: '部分维度分析尚未完成，正在打开现有报告。' },
  failed: { title: '本次分析暂未完成', detail: '已有工作记录和通话内容不会丢失，可以重新发起分析。' },
}

const terminalStages = new Set<ReportJob['stage']>(['succeeded', 'partial', 'failed'])

export function ReportProgressPage() {
  const { jobId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const job = useQuery({
    queryKey: ['report-job', jobId],
    queryFn: () => getReportJob(jobId),
    retry: false,
    refetchInterval: (query) => terminalStages.has(query.state.data?.stage ?? 'queued') ? false : 2000,
  })
  const retry = useMutation({
    mutationFn: () => retryReportJob(jobId),
    onSuccess: (nextJob) => queryClient.setQueryData(['report-job', jobId], nextJob),
  })

  useEffect(() => {
    if (
      job.data?.report_id
      && (job.data.stage === 'succeeded' || job.data.stage === 'partial')
    ) {
      navigate(`/reports/${job.data.report_id}`, { replace: true })
    }
  }, [job.data?.report_id, job.data?.stage, navigate])

  if (job.isPending) {
    return (
      <main className="report-progress-page page-enter" aria-live="polite">
        <div className="report-progress-mark" aria-hidden="true"><FileSearch /></div>
        <p className="archive-kicker">分析任务</p>
        <h1>正在读取任务状态</h1>
        <p>已保存的材料不会受到页面刷新的影响。</p>
      </main>
    )
  }

  if (job.isError || !job.data) {
    return (
      <main className="error-sheet page-enter">
        <h1>任务状态暂时无法读取</h1>
        <p>请检查本地服务连接后重新读取，已保存的通话和工作记录不会丢失。</p>
        <button className="button button--ink" type="button" onClick={() => void job.refetch()}>
          <RotateCcw size={16} aria-hidden="true" />重新读取
        </button>
      </main>
    )
  }

  const copy = stageCopy[job.data.stage]
  const failed = job.data.stage === 'failed'
  return (
    <main className="report-progress-page page-enter" aria-live="polite">
      <div className={`report-progress-mark${failed ? ' report-progress-mark--failed' : ''}`} aria-hidden="true">
        <FileSearch />
      </div>
      <p className="archive-kicker">分析任务 · {job.data.id}</p>
      <h1>{copy.title}</h1>
      <p>{copy.detail}</p>

      {!failed ? (
        <div className="report-progress-track" aria-label="报告分析进度">
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={job.data.progress_percent}
            style={{ width: `${job.data.progress_percent}%` }}
          />
        </div>
      ) : null}

      <p className="report-progress-note">
        {failed ? '本次状态不会影响受测者已完成的作答。' : '页面可安全刷新，系统会继续读取当前任务状态。'}
      </p>

      {failed && job.data.retryable ? (
        <div>
          {retry.isError ? <p className="record-error" role="alert">重新分析暂未发起，请稍后再试。</p> : null}
          <button className="button button--coral" type="button" disabled={retry.isPending} onClick={() => retry.mutate()}>
            <RotateCcw size={16} aria-hidden="true" />
            {retry.isPending ? '正在重新发起…' : '重新分析'}
          </button>
        </div>
      ) : null}
    </main>
  )
}
