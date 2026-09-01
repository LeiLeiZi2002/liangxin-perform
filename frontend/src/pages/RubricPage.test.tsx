/// <reference types="node" />

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { RubricPage } from './RubricPage'

const api = vi.hoisted(() => ({
  getRubricDocument: vi.fn(),
}))

vi.mock('../api/client', () => api)

const authoritativeMarkdown = readFileSync(
  resolve(process.cwd(), '../docs/热线心理支持职业胜任力测评量规.md'),
  'utf8',
)
const authoritativeDocument = {
  title: '热线心理支持职业胜任力测评量规',
  markdown: authoritativeMarkdown,
}
const originalScrollIntoView = Element.prototype.scrollIntoView
const scrollIntoView = vi.fn()

function renderPage() {
  const queryClient = new QueryClient()

  return render(
    <QueryClientProvider client={queryClient}>
      <RubricPage />
    </QueryClientProvider>,
  )
}

describe('完整量规页', () => {
  beforeEach(() => {
    api.getRubricDocument.mockReset()
    scrollIntoView.mockReset()
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
      writable: true,
    })
    window.history.replaceState(null, '', '/rubric')
  })

  afterAll(() => {
    if (originalScrollIntoView) {
      Object.defineProperty(Element.prototype, 'scrollIntoView', {
        configurable: true,
        value: originalScrollIntoView,
        writable: true,
      })
      return
    }
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('读取权威量规时显示清楚的加载状态', () => {
    api.getRubricDocument.mockImplementation(() => new Promise(() => undefined))

    renderPage()

    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('测评量规全文')
    expect(status).toHaveTextContent('正在读取完整量规')
    expect(status).toHaveTextContent('正在读取量规正文，请稍候。')
  })

  it('按权威 Markdown 完整呈现章节、评分单元、目录和专业依据', async () => {
    api.getRubricDocument.mockResolvedValue(authoritativeDocument)

    const { container } = renderPage()

    expect(await screen.findByRole('heading', {
      level: 1,
      name: authoritativeDocument.title,
    })).toBeInTheDocument()

    const masthead = container.querySelector('.rubric-page__masthead')
    expect(masthead).toHaveTextContent('测评量规全文')
    expect(masthead).toHaveTextContent(
      '本页面完整收录测评所依据的量规文本，供测评组织者、评分复核者和专业评审查阅。报告中的维度名称、测量内容和等级描述均以此为准。',
    )
    expect(masthead).not.toHaveTextContent(/版本|总分|百分制|合格|通过|达标|优秀/)

    const facts = screen.getByRole('list', { name: '量规事实摘要' })
    const [coreFact, moduleFact, levelFact] = within(facts).getAllByRole('listitem')
    expect(coreFact).toHaveTextContent(/9\s*项核心维度/)
    expect(moduleFact).toHaveTextContent(/9\s*项情景专项模块/)
    expect(levelFact).toHaveTextContent(/0—4\s*级行为描述/)

    const controls = screen.getByLabelText('量规内容显示控制')
    expect(controls).toHaveTextContent(
      '核心维度和情景专项模块可按需展开，一般章节始终显示。',
    )

    const generalSections = container.querySelectorAll('section.rubric-chapter')
    const coreUnits = container.querySelectorAll('details.rubric-unit[data-rubric-kind="core"]')
    const moduleUnits = container.querySelectorAll('details.rubric-unit[data-rubric-kind="module"]')
    const allUnits = container.querySelectorAll('details.rubric-unit')
    expect(generalSections).toHaveLength(11)
    expect(coreUnits).toHaveLength(9)
    expect(moduleUnits).toHaveLength(9)
    expect(allUnits).toHaveLength(18)

    const desktopToc = screen.getByRole('navigation', { name: '完整量规目录' })
    expect(within(desktopToc).getByText('一般章节')).toBeInTheDocument()
    expect(within(desktopToc).getByText('核心维度')).toBeInTheDocument()
    expect(within(desktopToc).getByText('情景专项模块')).toBeInTheDocument()
    expect(within(desktopToc).getAllByRole('link')).toHaveLength(29)
    expect(within(desktopToc).getByRole('link', {
      name: '一、用途与适用范围',
    })).toHaveAttribute('href', '#chapter-1')
    expect(within(desktopToc).getByRole('link', {
      name: 'C4 信息整合与专业判断',
    })).toHaveAttribute('href', '#C4')
    expect(container.querySelector('details.rubric-mobile-toc')).toBeInTheDocument()

    const c4 = document.getElementById('C4')
    expect(c4).toHaveTextContent('C4')
    expect(c4).toHaveTextContent('信息整合与专业判断')
    expect(c4).toHaveTextContent('核心维度')
    expect(c4).toHaveTextContent('面对信息不完整、相互矛盾或存在多种解释时')
    expect(document.getElementById('S1a')).toHaveTextContent('情景专项模块')
    expect(screen.getByRole('heading', { name: '十一、专业依据' })).toBeInTheDocument()

    const professionalBasis = document.getElementById('chapter-11')
    expect(professionalBasis).not.toBeNull()
    const externalLinks = within(professionalBasis as HTMLElement).getAllByRole('link')
    expect(externalLinks).toHaveLength(5)
    externalLinks.forEach((link) => {
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noreferrer')
    })

    within(c4 as HTMLElement).getAllByRole('columnheader').forEach((header) => {
      expect(header).toHaveAttribute('scope', 'col')
    })
    const firstTableWrap = container.querySelector('.rubric-table-wrap')
    expect(firstTableWrap).not.toHaveAttribute('role')
    expect(firstTableWrap).not.toHaveAttribute('aria-label')
    expect(firstTableWrap).not.toHaveAttribute('tabindex')
    expect(within(firstTableWrap as HTMLElement).getByRole('table')).toBeInTheDocument()
    expect(container.querySelector('.rubric-page')).not.toHaveTextContent(
      /rubric_fingerprint|model_fingerprint|prompt_fingerprint|\bgeneral\b|\bmodule\b/,
    )
  })

  it('可一次展开或收起全部十八个评分单元', async () => {
    api.getRubricDocument.mockResolvedValue(authoritativeDocument)
    const user = userEvent.setup()
    const { container } = renderPage()

    await screen.findByRole('heading', { level: 1, name: authoritativeDocument.title })
    const units = Array.from(
      container.querySelectorAll<HTMLDetailsElement>('details.rubric-unit'),
    )
    expect(units.every((unit) => !unit.open)).toBe(true)

    await user.click(screen.getByRole('button', { name: '展开全部' }))
    expect(units.every((unit) => unit.open)).toBe(true)

    await user.click(screen.getByRole('button', { name: '收起全部' }))
    expect(units.every((unit) => !unit.open)).toBe(true)
  })

  it('点击 C4 目录项时先展开对应评分单元并标记当前位置', async () => {
    api.getRubricDocument.mockResolvedValue(authoritativeDocument)
    const user = userEvent.setup()
    const { container } = renderPage()

    await screen.findByRole('heading', { level: 1, name: authoritativeDocument.title })
    const desktopToc = screen.getByRole('navigation', { name: '完整量规目录' })
    const link = within(desktopToc).getByRole('link', {
      name: 'C4 信息整合与专业判断',
    })
    const c4 = container.querySelector<HTMLDetailsElement>('#C4')
    expect(c4?.open).toBe(false)

    await user.click(link)

    expect(c4?.open).toBe(true)
    expect(link).toHaveAttribute('aria-current', 'location')
  })

  it('带 S1a 锚点进入页面时在数据加载后自动展开对应评分单元', async () => {
    window.history.replaceState(null, '', '/rubric#S1a')
    api.getRubricDocument.mockResolvedValue(authoritativeDocument)
    const { container } = renderPage()

    await screen.findByRole('heading', { level: 1, name: authoritativeDocument.title })

    const s1a = container.querySelector<HTMLDetailsElement>('#S1a')
    await waitFor(() => expect(s1a?.open).toBe(true))
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' })
    expect(scrollIntoView.mock.contexts[0]).toBe(s1a)
  })

  it('hashchange 到合法单元时更新目录、展开详情并滚动到目标', async () => {
    api.getRubricDocument.mockResolvedValue(authoritativeDocument)
    const { container } = renderPage()

    await screen.findByRole('heading', { level: 1, name: authoritativeDocument.title })
    scrollIntoView.mockClear()
    window.history.pushState(null, '', '/rubric#C4')
    window.dispatchEvent(new Event('hashchange'))

    const c4 = container.querySelector<HTMLDetailsElement>('#C4')
    const desktopToc = screen.getByRole('navigation', { name: '完整量规目录' })
    const c4Link = within(desktopToc).getByRole('link', {
      name: 'C4 信息整合与专业判断',
    })
    await waitFor(() => {
      expect(c4?.open).toBe(true)
      expect(c4Link).toHaveAttribute('aria-current', 'location')
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' })
    })
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(c4)
  })

  it('解析失败时说明量规无法读取，重新读取后可恢复', async () => {
    api.getRubricDocument
      .mockResolvedValueOnce({ title: '示例量规', markdown: '# 示例量规\n正文' })
      .mockResolvedValueOnce(authoritativeDocument)
    const user = userEvent.setup()

    renderPage()

    expect(await screen.findByRole('heading', {
      level: 1,
      name: '量规暂时无法读取',
    })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('测评量规全文')
    expect(screen.getByText(
      '量规正文的章节结构无法识别，请检查量规文件后重试。',
    )).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '重新读取' }))

    expect(await screen.findByRole('heading', {
      level: 1,
      name: authoritativeDocument.title,
    })).toBeInTheDocument()
    expect(api.getRubricDocument).toHaveBeenCalledTimes(2)
  })

  it('接口失败时提供重新读取入口，并在下一次成功后恢复全文', async () => {
    api.getRubricDocument
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce(authoritativeDocument)
    const user = userEvent.setup()

    renderPage()

    expect(await screen.findByRole('heading', {
      level: 1,
      name: '量规暂时无法读取',
    })).toBeInTheDocument()
    expect(screen.getByText(
      '当前无法读取量规正文，请检查本地服务连接后重试。',
    )).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '重新读取' }))

    expect(await screen.findByRole('heading', {
      level: 1,
      name: authoritativeDocument.title,
    })).toBeInTheDocument()
    expect(document.getElementById('chapter-11')).toHaveTextContent('专业依据')
    expect(api.getRubricDocument).toHaveBeenCalledTimes(2)
  })
})
