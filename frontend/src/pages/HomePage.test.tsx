import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { HomePage } from './HomePage'

describe('演示首页', () => {
  it('使用唯一的产品标题作为一级标题', () => {
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    expect(
      screen.getByRole('heading', {
        level: 1,
        name: '初阶心理服务从业者 · 胜任力测评',
      }),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('初阶心理服务从业者 · 胜任力测评', { selector: 'p' }),
    ).not.toBeInTheDocument()

    const title = screen.getByRole('heading', { level: 1 })
    expect(title.querySelector('span')).toBeNull()
  })

  it('首屏说明直接从受测者任务开始', () => {
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    expect(screen.queryByText(/把评价从/)).not.toBeInTheDocument()
    expect(screen.getByText(/^你将扮演心理工作者/)).toBeInTheDocument()
  })

  it('受测者首页只提供测评、体验和配置入口', () => {
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    const expectedLinks = [
      ['正式测评', '/assessment'],
      ['自由体验', '/experience'],
      ['任务配置', '/configure'],
    ] as const

    // 首屏另有一个「开始正式测评」行动按钮，因此把入口卡的断言限定在入口区内。
    const entryRegion = screen.getByRole('region', { name: '演示入口' })
    for (const [name, href] of expectedLinks) {
      expect(within(entryRegion).getByRole('link', { name: new RegExp(name) })).toHaveAttribute(
        'href',
        href,
      )
    }
    expect(within(entryRegion).queryByRole('link', { name: /专家复核/ })).not.toBeInTheDocument()
    expect(within(entryRegion).queryByText(/评分报告/)).not.toBeInTheDocument()
    expect(within(entryRegion).queryByText(/软时间/)).not.toBeInTheDocument()
    expect(within(entryRegion).getByRole('heading', { name: '三个入口，覆盖一次完整流程' })).toBeInTheDocument()
  })

  it('清楚说明三个独立场域、媒介与非固定回合规则', () => {
    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    const sceneRegion = screen.getByRole('region', { name: '测评场域' })
    const institutionHeading = within(sceneRegion).getByRole('heading', { name: '机构面谈' })
    expect(institutionHeading).toBeInTheDocument()
    expect(within(institutionHeading.closest('article')!).getByText('DEMO 暂未开放')).toBeVisible()
    expect(within(sceneRegion).getByRole('heading', { name: '心理热线' })).toBeInTheDocument()
    expect(within(sceneRegion).getByRole('heading', { name: '在线咨询' })).toBeInTheDocument()
    expect(within(sceneRegion).getAllByText('实时语音')).toHaveLength(2)
    expect(within(sceneRegion).getByText('实时文字')).toBeInTheDocument()
    for (const media of within(sceneRegion).getAllByText(/^实时(?:语音|文字)$/)) {
      expect(media).toHaveClass('scene-row__media')
    }
    expect(screen.getByText(/一次只进入一个场域/)).toBeInTheDocument()
    expect(screen.getByText(/不设置固定回合数/)).toBeInTheDocument()
  })
})
