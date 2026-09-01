import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { appRoutes } from '../app/router'

const api = vi.hoisted(() => ({
  getHealth: vi.fn(),
  getProviderConfig: vi.fn(),
}))
vi.mock('../api/client', () => api)

describe('测评完成页', () => {
  beforeEach(() => {
    api.getHealth.mockResolvedValue({ status: 'ready', service: 'psych-assessment-demo' })
    api.getProviderConfig.mockResolvedValue({ configured: true })
  })

  it('工作记录提交后只确认测评完成，不依赖报告状态', async () => {
    const router = createMemoryRouter(appRoutes, {
      initialEntries: [{
        pathname: '/sessions/session-1/complete',
        state: { workRecordSubmitted: true },
      }],
    })

    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: '本次测评已完成' })).toBeInTheDocument()
    expect(screen.getByText(/热线工作记录已经提交/)).toBeInTheDocument()
    expect(screen.queryByText(/报告|评分|总分/)).not.toBeInTheDocument()
  })

  it('直接进入完成页时不虚假宣称工作记录已提交', async () => {
    const router = createMemoryRouter(appRoutes, {
      initialEntries: ['/sessions/session-1/complete'],
    })

    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: '本次测评流程已结束' })).toBeInTheDocument()
    expect(screen.queryByText(/热线工作记录已经提交|已保存/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回会谈记录' })).toHaveAttribute(
      'href',
      '/session/session-1',
    )
  })

  it('全局导航不向受测者展示专家复核入口', async () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ['/'] })

    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', {
      name: '初阶心理服务从业者 · 胜任力测评',
    })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '专家复核' })).not.toBeInTheDocument()
  })
})
