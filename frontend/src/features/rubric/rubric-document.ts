export type RubricSectionKind = 'general' | 'core' | 'module'

export interface RubricSection {
  id: string
  code: string | null
  title: string
  heading: string
  kind: RubricSectionKind
  markdown: string
}

export interface ParsedRubricDocument {
  title: string
  sections: RubricSection[]
}

const documentTitlePattern = /^#[ \t]+([^\r\n]+?)[ \t]*(?=\r?$)/m
const sectionHeadingPattern = /^##[ \t]+([^\r\n]+?)[ \t]*(?=\r?$)/gm
const coreHeadingPattern = /^(C[1-9])[ \t]+(.+)$/
const moduleHeadingPattern = /^(S(?:1[ab]|[2-8]))[ \t]+(.+)$/
const chineseChapterHeadingPattern = /^[一二三四五六七八九十]+、[ \t]*(.+)$/

export function parseRubricDocument(markdown: string): ParsedRubricDocument {
  const titleMatch = documentTitlePattern.exec(markdown)

  if (!titleMatch) {
    throw new Error('量规 Markdown 缺少一级标题（# 标题）。')
  }

  const headings = [...markdown.matchAll(sectionHeadingPattern)]

  if (headings.length === 0) {
    throw new Error('量规 Markdown 缺少二级标题（## 标题）。')
  }

  let generalIndex = 0
  const sections = headings.map((headingMatch, index) => {
    const heading = headingMatch[1].trim()
    const contentStart = headingMatch.index + headingMatch[0].length
    const contentEnd = headings[index + 1]?.index ?? markdown.length
    const sectionMarkdown = markdown.slice(contentStart, contentEnd)
    const coreMatch = coreHeadingPattern.exec(heading)

    if (coreMatch) {
      return createCodedSection(
        coreMatch[1],
        coreMatch[2],
        heading,
        'core',
        sectionMarkdown,
      )
    }

    const moduleMatch = moduleHeadingPattern.exec(heading)

    if (moduleMatch) {
      return createCodedSection(
        moduleMatch[1],
        moduleMatch[2],
        heading,
        'module',
        sectionMarkdown,
      )
    }

    generalIndex += 1
    const chapterMatch = chineseChapterHeadingPattern.exec(heading)

    return {
      id: `chapter-${generalIndex}`,
      code: null,
      title: (chapterMatch?.[1] ?? heading).trim(),
      heading,
      kind: 'general' as const,
      markdown: sectionMarkdown,
    }
  })

  return {
    title: titleMatch[1].trim(),
    sections,
  }
}

function createCodedSection(
  code: string,
  title: string,
  heading: string,
  kind: Extract<RubricSectionKind, 'core' | 'module'>,
  markdown: string,
): RubricSection {
  return {
    id: code,
    code,
    title: title.trim(),
    heading,
    kind,
    markdown,
  }
}
