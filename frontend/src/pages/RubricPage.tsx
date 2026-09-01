import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { getRubricDocument } from '../api/client'
import {
  parseRubricDocument,
  type ParsedRubricDocument,
  type RubricSection,
} from '../features/rubric/rubric-document'

const rubricIntroduction =
  '本页面完整收录测评所依据的量规文本，供测评组织者、评分复核者和专业评审查阅。报告中的维度名称、测量内容和等级描述均以此为准。'

class RubricDocumentFormatError extends Error {
  constructor() {
    super('量规正文无法按章节结构解析。')
    this.name = 'RubricDocumentFormatError'
  }
}

interface LoadedRubricDocument {
  title: string
  parsed: ParsedRubricDocument
}

async function loadRubricDocument(): Promise<LoadedRubricDocument> {
  const document = await getRubricDocument()

  try {
    const parsed = parseRubricDocument(document.markdown)
    if (parsed.title !== document.title) {
      throw new RubricDocumentFormatError()
    }
    return { title: document.title, parsed }
  } catch (error) {
    if (error instanceof RubricDocumentFormatError) throw error
    throw new RubricDocumentFormatError()
  }
}

const markdownComponents: Components = {
  a({ node, href, ...properties }) {
    void node
    const isExternal = typeof href === 'string' && /^https?:\/\//i.test(href)

    return (
      <a
        {...properties}
        href={href}
        rel={isExternal ? 'noreferrer' : undefined}
        target={isExternal ? '_blank' : undefined}
      />
    )
  },
  table({ node, ...properties }) {
    void node
    return (
      <div className="rubric-table-wrap">
        <table {...properties} />
      </div>
    )
  },
  th({ node, ...properties }) {
    void node
    return <th {...properties} scope="col" />
  },
}

function MarkdownSection({ markdown }: { markdown: string }) {
  return (
    <div className="rubric-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {markdown}
      </ReactMarkdown>
    </div>
  )
}

interface RubricTableOfContentsProps {
  activeId: string | null
  label: string
  sections: RubricSection[]
  onNavigate: (id: string) => void
}

function RubricTableOfContents({
  activeId,
  label,
  sections,
  onNavigate,
}: RubricTableOfContentsProps) {
  const groups = [
    {
      label: '一般章节',
      sections: sections.filter((section) => section.kind === 'general'),
    },
    {
      label: '核心维度',
      sections: sections.filter((section) => section.kind === 'core'),
    },
    {
      label: '情景专项模块',
      sections: sections.filter((section) => section.kind === 'module'),
    },
  ]

  return (
    <nav className="rubric-toc" aria-label={label}>
      {groups.map((group) => (
        <section className="rubric-toc__group" key={group.label}>
          <h2>{group.label}</h2>
          <ol>
            {group.sections.map((section) => (
              <li key={section.id}>
                <a
                  aria-current={activeId === section.id ? 'location' : undefined}
                  href={`#${section.id}`}
                  onClick={() => onNavigate(section.id)}
                >
                  {section.heading}
                </a>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </nav>
  )
}

function RubricLoadingState() {
  return (
    <section className="rubric-page rubric-page--state page-enter">
      <div className="rubric-page__state" role="status" aria-live="polite">
        <span className="rubric-page__loading-mark" aria-hidden="true" />
        <p className="archive-kicker">测评量规全文</p>
        <h1>正在读取完整量规</h1>
        <p>正在读取量规正文，请稍候。</p>
      </div>
    </section>
  )
}

interface RubricErrorStateProps {
  isFetching: boolean
  isFormatError: boolean
  onRetry: () => void
}

function RubricErrorState({ isFetching, isFormatError, onRetry }: RubricErrorStateProps) {
  return (
    <section className="rubric-page rubric-page--state page-enter">
      <div className="rubric-page__state rubric-page__state--error" role="alert">
        <p className="archive-kicker">测评量规全文</p>
        <h1>量规暂时无法读取</h1>
        <p>
          {isFormatError
            ? '量规正文的章节结构无法识别，请检查量规文件后重试。'
            : '当前无法读取量规正文，请检查本地服务连接后重试。'}
        </p>
        <button
          className="button button--coral"
          disabled={isFetching}
          onClick={onRetry}
          type="button"
        >
          {isFetching ? '正在读取' : '重新读取'}
        </button>
      </div>
    </section>
  )
}

function readLocationHash(): string | null {
  const value = window.location.hash.slice(1)
  if (!value) return null

  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function findRubricUnit(
  container: HTMLElement | null,
  id: string,
): HTMLDetailsElement | undefined {
  if (!container) return undefined
  return Array.from(container.querySelectorAll<HTMLDetailsElement>('details.rubric-unit'))
    .find((unit) => unit.id === id)
}

function findRubricTarget(
  container: HTMLElement | null,
  id: string,
): HTMLElement | undefined {
  if (!container) return undefined
  return Array.from(
    container.querySelectorAll<HTMLElement>('.rubric-chapter, .rubric-unit'),
  ).find((target) => target.id === id)
}

export function RubricPage() {
  const pageRef = useRef<HTMLElement>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const rubricQuery = useQuery({
    queryKey: ['rubric-document'],
    queryFn: loadRubricDocument,
    retry: false,
  })

  useEffect(() => {
    const rubric = rubricQuery.data
    if (!rubric) return

    let frameId: number | null = null
    const syncHashTarget = () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId)
        frameId = null
      }

      const hashId = readLocationHash()
      if (!hashId || !rubric.parsed.sections.some((section) => section.id === hashId)) return

      frameId = window.requestAnimationFrame(() => {
        frameId = null
        const target = findRubricTarget(pageRef.current, hashId)
        if (!target) return

        setActiveId(hashId)
        if (target instanceof HTMLDetailsElement) target.open = true
        target.scrollIntoView({ block: 'start' })
      })
    }

    syncHashTarget()
    window.addEventListener('hashchange', syncHashTarget)
    return () => {
      window.removeEventListener('hashchange', syncHashTarget)
      if (frameId !== null) window.cancelAnimationFrame(frameId)
    }
  }, [rubricQuery.data])

  if (rubricQuery.isPending) return <RubricLoadingState />

  if (rubricQuery.isError) {
    return (
      <RubricErrorState
        isFetching={rubricQuery.isFetching}
        isFormatError={rubricQuery.error instanceof RubricDocumentFormatError}
        onRetry={() => { void rubricQuery.refetch() }}
      />
    )
  }

  const sections = rubricQuery.data.parsed.sections
  const coreCount = sections.filter((section) => section.kind === 'core').length
  const moduleCount = sections.filter((section) => section.kind === 'module').length

  const handleNavigate = (id: string) => {
    setActiveId(id)
    const unit = findRubricUnit(pageRef.current, id)
    if (unit) unit.open = true
  }

  const setAllUnitsOpen = (open: boolean) => {
    pageRef.current
      ?.querySelectorAll<HTMLDetailsElement>('details.rubric-unit')
      .forEach((unit) => { unit.open = open })
  }

  return (
    <article className="rubric-page page-enter" ref={pageRef}>
      <header className="rubric-page__masthead">
        <p className="archive-kicker">测评量规全文</p>
        <h1>{rubricQuery.data.title}</h1>
        <p className="rubric-page__introduction">{rubricIntroduction}</p>
        <ul className="rubric-page__facts" aria-label="量规事实摘要">
          <li><strong>{coreCount}</strong><span>项核心维度</span></li>
          <li><strong>{moduleCount}</strong><span>项情景专项模块</span></li>
          <li><strong>0—4</strong><span>级行为描述</span></li>
        </ul>
      </header>

      <div className="rubric-page__controls" aria-label="量规内容显示控制">
        <p>核心维度和情景专项模块可按需展开，一般章节始终显示。</p>
        <div>
          <button className="button" onClick={() => setAllUnitsOpen(true)} type="button">
            展开全部
          </button>
          <button className="button" onClick={() => setAllUnitsOpen(false)} type="button">
            收起全部
          </button>
        </div>
      </div>

      <details className="rubric-mobile-toc">
        <summary>浏览量规目录</summary>
        <RubricTableOfContents
          activeId={activeId}
          label="移动端完整量规目录"
          onNavigate={handleNavigate}
          sections={sections}
        />
      </details>

      <div className="rubric-page__layout">
        <aside className="rubric-page__sidebar">
          <RubricTableOfContents
            activeId={activeId}
            label="完整量规目录"
            onNavigate={handleNavigate}
            sections={sections}
          />
        </aside>

        <div className="rubric-page__document" aria-label="量规正文">
          {sections.map((section) => {
            if (section.kind === 'general') {
              return (
                <section className="rubric-chapter" id={section.id} key={section.id}>
                  <header>
                    <span aria-hidden="true">章</span>
                    <h2>{section.heading}</h2>
                  </header>
                  <MarkdownSection markdown={section.markdown} />
                </section>
              )
            }

            const category = section.kind === 'core' ? '核心维度' : '情景专项模块'
            return (
              <details
                className="rubric-unit"
                data-rubric-kind={section.kind}
                id={section.id}
                key={section.id}
              >
                <summary>
                  <h2>
                    <span className="rubric-unit__code">{section.code}</span>
                    <span className="rubric-unit__title">{section.title}</span>
                    <span className="rubric-unit__category">{category}</span>
                  </h2>
                </summary>
                <div className="rubric-unit__body">
                  <MarkdownSection markdown={section.markdown} />
                </div>
              </details>
            )
          })}
        </div>
      </div>
    </article>
  )
}
