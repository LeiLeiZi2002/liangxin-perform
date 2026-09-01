/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { parseRubricDocument } from './rubric-document'

const rubricPath = resolve(process.cwd(), '../docs/热线心理支持职业胜任力测评量规.md')

describe('parseRubricDocument', () => {
  it('按真实权威量规的二级标题解析全部章节', () => {
    const document = parseRubricDocument(readFileSync(rubricPath, 'utf8'))
    const coreSections = document.sections.filter((section) => section.kind === 'core')
    const moduleSections = document.sections.filter((section) => section.kind === 'module')
    const generalSections = document.sections.filter((section) => section.kind === 'general')
    const c4 = document.sections.find((section) => section.id === 'C4')
    const s1a = document.sections.find((section) => section.id === 'S1a')

    expect(document.title).toBe('热线心理支持职业胜任力测评量规')
    expect(generalSections).toHaveLength(11)
    expect(coreSections).toHaveLength(9)
    expect(moduleSections).toHaveLength(9)
    expect(c4).toMatchObject({
      id: 'C4',
      code: 'C4',
      title: '信息整合与专业判断',
      heading: 'C4 信息整合与专业判断',
      kind: 'core',
    })
    expect(s1a).toMatchObject({
      id: 'S1a',
      code: 'S1a',
      title: '基础风险筛查',
      heading: 'S1a 基础风险筛查',
      kind: 'module',
    })
    expect(c4?.markdown).toContain('### 等级锚点')
    const expectedC4LevelAnchors = [
      '| 0 | 作出明显缺乏依据的诊断或结论；编造事实；遗漏已经明确出现的紧迫问题并据此采取不当行动。 |',
      '| 1 | 信息零散且缺少关联，主要重复单项事实；判断依赖直觉、标签或单一原因，未说明不确定性。 |',
      '| 2 | 能概括主要事件、情绪和部分功能影响，形成基本可理解的判断；信息整合、证据边界或修正能力仍不完整。 |',
      '| 3 | 能整合事件、体验、功能、应对和支持信息，清楚区分事实与推断，并根据新信息调整工作理解和优先事项。 |',
      '| 4 | 面对信息不完整、相互矛盾或存在多种解释时，能够保留合理假设、比较证据并形成清楚、可修正且适合热线职责的判断。 |',
    ]

    expectedC4LevelAnchors.forEach((anchor) => {
      expect(c4?.markdown).toContain(anchor)
    })
    expect(c4?.markdown).toContain(
      '3级中的“根据新信息调整工作理解和优先事项”为条件行为，需要通话中实际出现新事实、矛盾信息或来电者纠正；没有机会时依据其余必需行为定级。',
    )
  })

  it('解析 CRLF 输入且不改写章节正文和表格', () => {
    const markdown = [
      '# 示例量规',
      '',
      '## 一、总则',
      '原始段落：保留  空格。',
      '',
      '| 列A | 列B |',
      '|---|:---:|',
      '| 原样 | 内容 |',
      '',
      '### 三级标题',
      '保留原文。',
      '## C4 信息整合与专业判断',
      '核心正文。',
      '## S1a 基础风险筛查',
      '模块正文。',
    ].join('\r\n')

    const document = parseRubricDocument(markdown)

    expect(document.sections).toEqual([
      {
        id: 'chapter-1',
        code: null,
        title: '总则',
        heading: '一、总则',
        kind: 'general',
        markdown:
          '\r\n原始段落：保留  空格。\r\n\r\n| 列A | 列B |\r\n|---|:---:|\r\n| 原样 | 内容 |\r\n\r\n### 三级标题\r\n保留原文。\r\n',
      },
      {
        id: 'C4',
        code: 'C4',
        title: '信息整合与专业判断',
        heading: 'C4 信息整合与专业判断',
        kind: 'core',
        markdown: '\r\n核心正文。\r\n',
      },
      {
        id: 'S1a',
        code: 'S1a',
        title: '基础风险筛查',
        heading: 'S1a 基础风险筛查',
        kind: 'module',
        markdown: '\r\n模块正文。',
      },
    ])
  })

  it('保留编码章节的完整二级标题文字', () => {
    const document = parseRubricDocument('# 示例量规\n## C4   信息整合与专业判断\n正文')

    expect(document.sections[0]).toMatchObject({
      heading: 'C4   信息整合与专业判断',
      title: '信息整合与专业判断',
      markdown: '\n正文',
    })
  })

  it('在缺少一级标题或二级标题时抛出清晰错误', () => {
    expect(() => parseRubricDocument('## 一、总则\n正文')).toThrow('一级标题')
    expect(() => parseRubricDocument('# 示例量规\n正文')).toThrow('二级标题')
  })
})
