import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const authoritativeRubric = {
  title: '热线心理支持职业胜任力测评量规',
  markdown: readFileSync(
    resolve(process.cwd(), '../docs/热线心理支持职业胜任力测评量规.md'),
    'utf8',
  ),
}

test('保存内存凭证后创建会话并进入环境检查', async ({ page }) => {
  await page.goto('/configure')

  await expect(page.getByRole('heading', { name: '任务配置' })).toBeVisible()
  await page.getByLabel('百炼 API Key').fill('test-e2e-memory-only')
  await page.getByRole('button', { name: '保存服务配置' }).click()

  await expect(page.getByText('模型与语音服务配置已保存。')).toBeVisible()
  await expect(page.getByLabel('系统运行状态').getByText('已配置')).toBeVisible()

  await page.getByRole('link', { name: '正式测评', exact: true }).click()
  await expect(page.getByRole('heading', { name: '正式测评准备' })).toBeVisible()
  await expect(page.getByText('心理热线 · 主个案')).toBeVisible()
  await expect(page.getByRole('radio')).toHaveCount(0)
  await page.getByRole('button', { name: '开始正式测评' }).click()

  await expect(page).toHaveURL(/\/device-check\?.*scene=hotline/)
  await expect(page.getByRole('heading', { name: '先确认这通热线能顺利接通' })).toBeVisible()
  await expect(page.getByRole('button', { name: '接听来电' })).toBeDisabled()
})

test('完整量规在桌面与移动视口中可浏览且不会撑破页面', async ({ page }) => {
  await page.route('**/api/rubric', async (route) => {
    await route.fulfill({
      body: JSON.stringify(authoritativeRubric),
      contentType: 'application/json',
      status: 200,
    })
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/rubric#C4')

  const rubricPage = page.locator('.rubric-page')
  const sidebar = rubricPage.locator('.rubric-page__sidebar')
  const units = rubricPage.locator('details.rubric-unit')
  const c4 = rubricPage.locator('#C4')

  await expect(page.getByRole('heading', {
    level: 1,
    name: authoritativeRubric.title,
  })).toBeVisible()
  await expect(page.getByLabel('量规内容显示控制')).toContainText('情景专项模块')
  await expect(sidebar).toBeVisible()
  expect(await sidebar.evaluate((element) => getComputedStyle(element).position)).toBe('sticky')
  await expect(units).toHaveCount(18)
  await expect.poll(() => c4.evaluate((element) => (element as HTMLDetailsElement).open))
    .toBe(true)
  await expect.poll(() => c4.evaluate((element) => {
    const top = element.getBoundingClientRect().top
    return top >= 0 && top < window.innerHeight
  })).toBe(true)

  const c4DirectoryLink = sidebar.getByRole('link', { name: 'C4 信息整合与专业判断' })
  await page.keyboard.press('Tab')
  await c4DirectoryLink.focus()
  expect(await c4DirectoryLink.evaluate((element) => element === document.activeElement))
    .toBe(true)
  const focusOutline = await c4DirectoryLink.evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
    }
  })
  expect(focusOutline.style).not.toBe('none')
  expect(focusOutline.width).toBeGreaterThanOrEqual(2)

  const summaryHeight = await units.first().locator('summary').evaluate(
    (summary) => summary.getBoundingClientRect().height,
  )
  expect(summaryHeight).toBeGreaterThanOrEqual(52)

  await page.getByRole('button', { name: '展开全部' }).click()
  await expect.poll(() => units.evaluateAll(
    (elements) => elements.every((element) => (element as HTMLDetailsElement).open),
  )).toBe(true)

  await page.getByRole('button', { name: '收起全部' }).click()
  await expect.poll(() => units.evaluateAll(
    (elements) => elements.every((element) => !(element as HTMLDetailsElement).open),
  )).toBe(true)

  await page.setViewportSize({ width: 390, height: 844 })

  await expect(sidebar).toBeHidden()
  await expect(rubricPage.locator('details.rubric-mobile-toc')).toBeVisible()
  const pageWidth = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }))
  expect(pageWidth.scroll).toBe(pageWidth.client)

  const firstTable = rubricPage.locator('.rubric-table-wrap').first()
  const tableWidth = await firstTable.evaluate((table) => ({
    client: table.clientWidth,
    right: table.getBoundingClientRect().right,
    scroll: table.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }))
  expect(tableWidth.right).toBeLessThanOrEqual(tableWidth.viewport)
  expect(tableWidth.scroll).toBeLessThanOrEqual(tableWidth.client)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await rubricPage.evaluate((element) => getComputedStyle(element).animationName))
    .toBe('none')
})

test('在线咨询使用消息工作台并在窄屏自然堆叠', async ({ page }) => {
  const now = '2026-08-31T12:00:00Z'
  let transcript = [{
    id: 'client-opening',
    sequence: 1,
    speaker: 'client',
    text: '第一句\n第二句\n第三句',
    client_turn_id: 'opening-online',
    provider: 'qwen-plus-character',
    degraded: false,
    created_at: now,
    audio_available: false,
  }]
  await page.route('**/api/sessions/session-online', async (route) => {
    await route.fulfill({
      body: JSON.stringify({
        session: {
          id: 'session-online', mode: 'assessment', scene: 'online', case_type: 'main',
          case_id: 'marriage_boundary_main', media: 'text', status: 'active',
          model_mode: 'live', soft_duration_minutes: null, created_at: now, updated_at: now,
          ended_at: null, end_reason: null,
        },
        transcript,
      }),
      contentType: 'application/json',
      status: 200,
    })
  })
  await page.routeWebSocket('**/api/live-sessions/session-online', (socket) => {
    socket.onMessage((message) => {
      const event = JSON.parse(String(message)) as {
        type?: string
        client_turn_id?: string
        text?: string
      }
      if (event.type === 'session.start') {
        socket.send(JSON.stringify({
          type: 'snapshot', media: 'text', phase: 'listening', transcript,
        }))
        return
      }
      if (event.type !== 'text.turn' || !event.client_turn_id) return
      const visitorText = '我就是怕自己想多了。\n\n可那个画面一直在脑子里转。\n今晚我也不想马上跟他吵。'
      const worker = {
        id: 'worker-online-reply', sequence: 2, speaker: 'worker',
        text: event.text ?? '', client_turn_id: event.client_turn_id,
        provider: null, degraded: false, created_at: now, audio_available: false,
      }
      const client = {
        id: 'client-online-reply', sequence: 3, speaker: 'client',
        text: visitorText, client_turn_id: event.client_turn_id,
        provider: 'qwen-plus-character', degraded: false, created_at: now,
        audio_available: false,
      }
      transcript = [...transcript, worker, client]
      socket.send(JSON.stringify({ type: 'visitor.text', text: visitorText }))
      socket.send(JSON.stringify({
        type: 'turn.committed',
        client_turn_id: event.client_turn_id,
        worker,
        client,
      }))
    })
  })

  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/session/session-online')

  const workbench = page.getByRole('region', { name: '在线咨询工作台' })
  const messages = page.getByRole('region', { name: '在线咨询消息' })
  await expect(workbench).toBeVisible()
  await expect(messages.locator('[data-turn-id="client-opening"] .online-message-bubble'))
    .toHaveCount(3)
  await expect(page.getByRole('textbox', { name: '输入本轮内容' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '我说完了' })).toHaveCount(0)
  expect(await workbench.evaluate((element) => getComputedStyle(element).display)).toBe('grid')

  await page.getByRole('textbox', { name: '输入本轮内容' }).fill(
    '我先不替你下结论。你现在最怕发生什么？',
  )
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(messages.locator('[data-turn-id="worker-online-reply"]'))
    .toContainText('我先不替你下结论')
  const generatedReply = messages.locator('[data-turn-id="client-online-reply"]')
  await expect(generatedReply).toHaveCount(1)
  await expect(generatedReply.locator('.online-message-bubble')).toHaveCount(3)
  await expect(generatedReply).toContainText('今晚我也不想马上跟他吵')

  await page.reload()
  const restoredReply = page
    .getByRole('region', { name: '在线咨询消息' })
    .locator('[data-turn-id="client-online-reply"]')
  await expect(restoredReply).toHaveCount(1)
  await expect(restoredReply.locator('.online-message-bubble')).toHaveCount(3)
  await expect(page.locator('.voice-controls')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '我说完了' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: '在线咨询工作台' }))
    .not.toContainText(/心理热线|接线|来电|通话/)

  await page.setViewportSize({ width: 390, height: 844 })
  const width = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }))
  expect(width.scroll).toBe(width.client)
  expect(await page.locator('.online-chat-controls').evaluate(
    (element) => getComputedStyle(element).flexDirection,
  )).toBe('column')
})

test('心理热线将通话操作与原文并排并保留人工提交入口', async ({ page }) => {
  const now = '2026-08-31T12:00:00Z'
  const transcript = [{
    id: 'client-hotline-opening',
    sequence: 1,
    speaker: 'client',
    text: '喂，你好。我这几天因为家里的事一直睡不好。',
    client_turn_id: 'opening-hotline',
    provider: 'qwen-plus-character',
    degraded: false,
    created_at: now,
    audio_available: true,
  }]
  await page.route('**/api/sessions/session-hotline', async (route) => {
    await route.fulfill({
      body: JSON.stringify({
        session: {
          id: 'session-hotline', mode: 'assessment', scene: 'hotline', case_type: 'main',
          case_id: 'marriage_boundary_main', media: 'voice', status: 'active',
          model_mode: 'live', soft_duration_minutes: null, created_at: now, updated_at: now,
          ended_at: null, end_reason: null,
        },
        transcript,
      }),
      contentType: 'application/json',
      status: 200,
    })
  })
  await page.routeWebSocket('**/api/live-sessions/session-hotline', (socket) => {
    socket.onMessage((message) => {
      const event = JSON.parse(String(message)) as { type?: string }
      if (event.type !== 'session.start') return
      socket.send(JSON.stringify({
        type: 'snapshot', media: 'voice', phase: 'listening', transcript,
        can_retry: true, can_redo_input: true,
      }))
    })
  })

  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto('/session/session-hotline')

  const callControls = page.getByRole('region', { name: '通话与控制' })
  const transcriptSheet = page.getByRole('region', { name: '会谈原文' })
  await expect(callControls).toBeVisible()
  await expect(transcriptSheet).toContainText('我这几天因为家里的事一直睡不好')
  await expect(page.getByRole('button', { name: '我说完了' })).toBeVisible()
  await expect(page.getByRole('button', { name: '重新说这句' })).toBeVisible()
  const desktopBoxes = await Promise.all([
    callControls.boundingBox(),
    transcriptSheet.boundingBox(),
  ])
  expect(desktopBoxes[0]).not.toBeNull()
  expect(desktopBoxes[1]).not.toBeNull()
  expect(desktopBoxes[0]!.x).toBeLessThan(desktopBoxes[1]!.x)

  await page.setViewportSize({ width: 390, height: 844 })
  const mobileBoxes = await Promise.all([
    callControls.boundingBox(),
    transcriptSheet.boundingBox(),
  ])
  expect(mobileBoxes[0]).not.toBeNull()
  expect(mobileBoxes[1]).not.toBeNull()
  expect(mobileBoxes[0]!.y).toBeLessThan(mobileBoxes[1]!.y)
  const width = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }))
  expect(width.scroll).toBe(width.client)
})
