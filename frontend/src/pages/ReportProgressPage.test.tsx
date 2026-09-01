import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ReportProgressPage } from './ReportProgressPage'

const api = vi.hoisted(() => ({ getReportJob: vi.fn(), retryReportJob: vi.fn() }))
vi.mock('../api/client', () => api)

const baseJob = {
  id: 'job-1', session_id: 'session-1', stage: 'queued', progress_percent: 0,
  partial: false, retryable: false, report_id: null,
  created_at: '2026-08-30T00:00:00Z', updated_at: '2026-08-30T00:00:00Z',
}

function Location() {
  return <output>{useLocation().pathname}</output>
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })}>
      <MemoryRouter initialEntries={['/report-jobs/job-1']}>
        <Routes>
          <Route path="/report-jobs/:jobId" element={<ReportProgressPage />} />
          <Route path="/reports/:reportId" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('报告生成进度页', () => {
  beforeEach(() => vi.clearAllMocks())

  it('每两秒轮询并用具体阶段文案展示进度，成功后进入报告', async () => {
    api.getReportJob
      .mockResolvedValueOnce(baseJob)
      .mockResolvedValueOnce({
        ...baseJob, stage: 'succeeded', progress_percent: 100, report_id: 'report-1',
      })
    renderPage()

    expect(await screen.findByRole('heading', { name: '正在准备分析材料' })).toBeInTheDocument()
    expect(screen.queryByText(/剩余|预计|模型失败|系统崩溃/)).not.toBeInTheDocument()

    expect(await screen.findByText('/reports/report-1', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(api.getReportJob).toHaveBeenCalledTimes(2)
  }, 7000)

  it('部分完成且已有报告时直接进入报告', async () => {
    api.getReportJob.mockResolvedValue({
      ...baseJob, stage: 'partial', progress_percent: 100, partial: true,
      retryable: true, report_id: 'report-partial',
    })
    renderPage()

    expect(await screen.findByText('/reports/report-partial')).toBeInTheDocument()
  })

  it('失败时不暴露内部归因，可重试后继续轮询', async () => {
    const user = userEvent.setup()
    api.getReportJob.mockResolvedValue({ ...baseJob, stage: 'failed', retryable: true })
    api.retryReportJob.mockResolvedValue({
      ...baseJob, stage: 'coding', progress_percent: 20, retryable: false,
    })
    renderPage()

    expect(await screen.findByRole('heading', { name: '本次分析暂未完成' })).toBeInTheDocument()
    expect(screen.getByText('已有工作记录和通话内容不会丢失，可以重新发起分析。')).toBeInTheDocument()
    expect(screen.queryByText(/模型失败|系统崩溃|受测者材料有问题/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '重新分析' }))

    expect(api.retryReportJob).toHaveBeenCalledWith('job-1')
    expect(await screen.findByRole('heading', { name: '正在整理通话与工作记录' })).toBeInTheDocument()
  })
})
