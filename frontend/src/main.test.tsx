import { act } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

const entryState = vi.hoisted(() => ({
  appRootRenders: vi.fn(),
  roots: [] as Array<{ unmount: () => void }>,
}))

vi.mock('./App', () => ({
  default: () => {
    entryState.appRootRenders()
    return null
  },
}))

vi.mock('react-dom/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-dom/client')>()
  return {
    ...actual,
    createRoot: (container: Element | DocumentFragment) => {
      const root = actual.createRoot(container)
      entryState.roots.push(root)
      return root
    },
  }
})

beforeEach(() => {
  vi.resetModules()
  entryState.appRootRenders.mockClear()
  entryState.roots.length = 0
  document.body.innerHTML = '<div id="root"></div>'
})

afterEach(async () => {
  const root = entryState.roots.pop()
  if (root) await act(async () => root.unmount())
})

it('开发入口只构造一次 App，避免重复执行热线连接副作用', async () => {
  await act(async () => {
    await import('./main')
  })

  expect(entryState.roots).toHaveLength(1)
  expect(entryState.appRootRenders).toHaveBeenCalledTimes(1)
})
