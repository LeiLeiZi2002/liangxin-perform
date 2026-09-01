#!/usr/bin/env node

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import process from 'node:process'
import { DatabaseSync } from 'node:sqlite'
import { fileURLToPath } from 'node:url'

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(scriptDirectory, '..')
const frontendDirectory = path.join(repositoryRoot, 'frontend')
const backendDirectory = path.join(repositoryRoot, 'backend')
const databasePath = path.join(backendDirectory, 'data', 'demo.db')
const wavPath = path.join(
  backendDirectory,
  'data',
  'audio',
  '4840418fa2fd4beb85b85ad977485f0e',
  'ff250493811e436db9f563aebd911177.wav',
)
const casePath = path.join(
  backendDirectory,
  'app',
  'cases',
  'data',
  'marriage_boundary_main',
  'case.json',
)

const frontendOrigin = 'http://127.0.0.1:5173'
const backendOrigin = 'http://127.0.0.1:8000'
const caseId = 'marriage_boundary_main'
const expectedWavSha256 = '23839f16bb8ebafbafdebc57e4ce859085576a19f09bd11170c77592d94a73e9'
const auditNotice = '有效 ASR 话轮数是根据本地数据库已提交的 ASR 工作者语音话轮计数，是可审计口径，不是上游 WebSocket 建连次数。'
const timeouts = Object.freeze({
  fetch: 5_000,
  navigation: 20_000,
  browserAction: 10_000,
  wavInjection: 20_000,
  failureFreeze: 2_000,
  readiness: 20_000,
  openingRound: 180_000,
  asrTranscript: 30_000,
  replyRound: 240_000,
  manualEnd: 5 * 60_000,
})

class AcceptanceError extends Error {
  constructor(message) {
    super(message)
    this.name = 'AcceptanceError'
  }
}

function ensure(condition, message) {
  if (!condition) throw new AcceptanceError(message)
}

function parseMode(args) {
  if (args.length === 0) return 'preflight'
  if (args.length === 1 && args[0] === '--execute-once') return 'execute-once'
  if (args.length === 1 && args[0] === '--self-test') return 'self-test'
  if (args.length === 1 && (args[0] === '--help' || args[0] === '-h')) return 'help'
  throw new AcceptanceError(
    '参数不正确。默认只做零成本 preflight；真实执行必须显式传入 --execute-once。',
  )
}

function printUsage() {
  console.log(`用法：
  node scripts/accept-marriage-voice-once.mjs
      只做零成本 preflight，不创建会话、不打开浏览器、不调用模型。

  node scripts/accept-marriage-voice-once.mjs --execute-once
      真实执行恰好一次语音往返；必须在可交互终端中运行。

  node scripts/accept-marriage-voice-once.mjs --self-test
      只运行脚本内置的零成本逻辑自检。`)
}

function printableError(error) {
  if (error instanceof Error) return { name: error.name, message: error.message }
  return { name: 'Error', message: String(error) }
}

function printJson(payload) {
  console.log(JSON.stringify(payload, null, 2))
}

function parseJsonArray(value, fieldName) {
  if (Array.isArray(value)) return value
  if (value === null || value === undefined || value === '') return []
  if (typeof value !== 'string') {
    throw new AcceptanceError(`${fieldName} 不是可识别的 JSON 数组。`)
  }
  let parsed
  try {
    parsed = JSON.parse(value)
  } catch {
    throw new AcceptanceError(`${fieldName} 不是有效 JSON。`)
  }
  ensure(Array.isArray(parsed), `${fieldName} 必须是 JSON 数组。`)
  return parsed
}

function summarizeModelRole(modelCalls, role) {
  const selected = modelCalls.filter((item) => item.model_role === role)
  const succeeded = selected.filter((item) => Boolean(item.success)).length
  return {
    total: selected.length,
    success: succeeded,
    failure: selected.length - succeeded,
    repair: selected.filter((item) => item.call_kind === 'repair').length,
  }
}

function assertStageCallCounts(modelCalls, { actorMax, ttsMax }) {
  const allowedRoles = new Set(['actor', 'tts'])
  const unexpected = [...new Set(
    modelCalls.map((call) => call.model_role).filter((role) => !allowedRoles.has(role)),
  )]
  ensure(unexpected.length === 0, `当前阶段出现意外模型角色：${unexpected.join(', ')}。`)
  const counts = {
    actor: summarizeModelRole(modelCalls, 'actor'),
    tts: summarizeModelRole(modelCalls, 'tts'),
  }
  for (const [role, maximum] of [['actor', actorMax], ['tts', ttsMax]]) {
    const current = counts[role]
    ensure(
      current.failure === 0 && current.repair === 0,
      `${role} 已出现失败或 repair 调用。`,
    )
    ensure(current.total <= maximum, `${role} 调用数超出当前阶段上限 ${maximum}。`)
  }
  return counts
}

function summarizeEvidence(raw) {
  const modelCalls = raw.modelCalls ?? []
  const turns = raw.turns ?? []
  const speechMetrics = raw.speechMetrics ?? []
  const failures = raw.failures ?? []
  const audioRecords = raw.audioRecords ?? []
  const asrTurns = turns.filter(
    (turn) => turn.speaker === 'worker' && turn.provider === 'asr' && Boolean(turn.audio_path),
  )
  const audioRecordCounts = {}
  for (const record of audioRecords) {
    const key = String(record.kind ?? 'unknown')
    audioRecordCounts[key] = (audioRecordCounts[key] ?? 0) + 1
  }
  const knownRoles = new Set(['actor', 'tts'])
  return {
    session: raw.session ?? null,
    transcript_speakers: turns.map((turn) => turn.speaker),
    asr_transcript: asrTurns.map((turn) => String(turn.text ?? '').trim()).filter(Boolean),
    asr_effective_turns: asrTurns.length,
    asr_effective_turn_definition: auditNotice,
    asr_sentence_count: speechMetrics.reduce(
      (count, item) => count + parseJsonArray(item.asr_sentences_json, 'asr_sentences_json').length,
      0,
    ),
    call_counts: {
      actor: summarizeModelRole(modelCalls, 'actor'),
      tts: summarizeModelRole(modelCalls, 'tts'),
    },
    unexpected_model_call_roles: [
      ...new Set(modelCalls.map((item) => item.model_role).filter((role) => !knownRoles.has(role))),
    ],
    audio_record_counts: audioRecordCounts,
    failure_records: failures.map((failure) => ({
      component: failure.component,
      phase: failure.phase,
      operation: failure.operation,
      failure_code: failure.failure_code,
      error_class: failure.error_class,
      attempt_count: failure.attempt_count,
      retryable: Boolean(failure.retryable),
      disposition: failure.disposition,
      provider_status_code: failure.provider_status_code,
      created_at: failure.created_at,
    })),
  }
}

async function withFakeBrowserGlobals(test) {
  const names = ['navigator', 'AudioContext', 'webkitAudioContext', 'WebSocket', '__voiceAcceptance']
  const descriptors = new Map(
    names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]),
  )
  const tracks = []
  const contexts = []
  const sources = []
  const sockets = []
  const controls = { decodeHangs: false }

  class FakeTrack {
    constructor() {
      this.readyState = 'live'
      this.stopCalls = 0
      tracks.push(this)
    }

    stop() {
      this.stopCalls += 1
      this.readyState = 'ended'
    }
  }

  class FakeNode {
    connect() {}
    disconnect() {}
  }

  class FakeSource extends FakeNode {
    constructor() {
      super()
      this.onended = null
      this.loop = false
      this.stopCalls = 0
      sources.push(this)
    }

    start() {}

    stop() {
      this.stopCalls += 1
    }
  }

  class FakeAudioContext {
    constructor() {
      this.state = 'running'
      this.closeCalls = 0
      this.suspendCalls = 0
      contexts.push(this)
    }

    createMediaStreamDestination() {
      const track = new FakeTrack()
      return { stream: { getAudioTracks: () => [track], getTracks: () => [track] } }
    }

    createConstantSource() {
      return Object.assign(new FakeNode(), { start() {}, stop() {} })
    }

    createGain() {
      return Object.assign(new FakeNode(), { gain: { value: 1 } })
    }

    createBufferSource() {
      return new FakeSource()
    }

    async decodeAudioData() {
      if (controls.decodeHangs) return await new Promise(() => {})
      return { duration: 0.01 }
    }

    async resume() {
      this.state = 'running'
    }

    async suspend() {
      this.suspendCalls += 1
      this.state = 'suspended'
    }

    async close() {
      this.closeCalls += 1
      this.state = 'closed'
    }
  }

  class FakeWebSocket {
    static CONNECTING = 0
    static OPEN = 1
    static CLOSING = 2
    static CLOSED = 3

    constructor() {
      this.readyState = FakeWebSocket.OPEN
      this.closeCalls = 0
      sockets.push(this)
    }

    addEventListener() {}

    close() {
      this.closeCalls += 1
      this.readyState = FakeWebSocket.CLOSED
    }
  }

  const define = (name, value) => Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value,
  })
  define('navigator', { mediaDevices: { getUserMedia: async () => { throw new Error('unexpected') } } })
  define('AudioContext', FakeAudioContext)
  define('webkitAudioContext', undefined)
  define('WebSocket', FakeWebSocket)
  Reflect.deleteProperty(globalThis, '__voiceAcceptance')

  try {
    await test({ contexts, controls, sources, sockets, tracks })
  } finally {
    for (const name of names) {
      const descriptor = descriptors.get(name)
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else Reflect.deleteProperty(globalThis, name)
    }
  }
}

async function runVirtualMicrophoneTimeoutSelfTest() {
  await withFakeBrowserGlobals(async ({ contexts, sources, tracks }) => {
    installVirtualMicrophone({ wavBase64: 'AAAA', injectionTimeoutMs: 5 })
    await navigator.mediaDevices.getUserMedia({ audio: true })
    await assert.rejects(
      Promise.race([
        globalThis.__voiceAcceptance.playOnce(),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error('契约测试外层超时')),
          50,
        )),
      ]),
      /WAV 注入超时/,
    )
    assert.equal(sources.at(-1)?.stopCalls, 1)
    assert.equal(tracks.at(-1)?.stopCalls, 1)
    assert.equal(contexts.at(-1)?.closeCalls, 1)
  })
}

async function runVirtualMicrophoneDecodeTimeoutSelfTest() {
  await withFakeBrowserGlobals(async ({ contexts, controls, sources, tracks }) => {
    installVirtualMicrophone({ wavBase64: 'AAAA', injectionTimeoutMs: 5 })
    await navigator.mediaDevices.getUserMedia({ audio: true })
    controls.decodeHangs = true
    await assert.rejects(
      Promise.race([
        globalThis.__voiceAcceptance.playOnce(),
        new Promise((_, reject) => setTimeout(
          () => reject(new Error('解码契约测试外层超时')),
          50,
        )),
      ]),
      /WAV 注入超时/,
    )
    assert.equal(sources.length, 0)
    assert.equal(tracks.at(-1)?.stopCalls, 1)
    assert.equal(contexts.at(-1)?.closeCalls, 1)
  })
}

async function runFailureFreezeSelfTest() {
  await withFakeBrowserGlobals(async ({ contexts, sockets, sources, tracks }) => {
    installVirtualMicrophone({ wavBase64: 'AAAA', injectionTimeoutMs: 50 })
    const socket = new globalThis.WebSocket('ws://local.test')
    await navigator.mediaDevices.getUserMedia({ audio: true })
    const playback = globalThis.__voiceAcceptance.playOnce()
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(typeof globalThis.__voiceAcceptance.freeze, 'function')
    const first = await globalThis.__voiceAcceptance.freeze()
    const second = await globalThis.__voiceAcceptance.freeze()
    await assert.rejects(playback, /验收失败已冻结/)
    assert.equal(first.frozen, true)
    assert.equal(second.frozen, true)
    assert.equal(socket.closeCalls, 1)
    assert.equal(sockets.length, 1)
    assert.equal(sources.at(-1)?.stopCalls, 1)
    assert.equal(tracks.at(-1)?.stopCalls, 1)
    assert.equal(contexts.at(-1)?.closeCalls, 1)
    assert.throws(() => new globalThis.WebSocket('ws://retry.test'), /验收已冻结/)
    await assert.rejects(
      navigator.mediaDevices.getUserMedia({ audio: true }),
      /验收已冻结/,
    )
  })
}

async function runFailureCleanupSelfTest() {
  const events = []
  const page = {
    isClosed: () => false,
    evaluate: async () => {
      events.push('freeze')
      return { frozen: true }
    },
    close: async () => { events.push('page.close') },
  }
  const browser = { close: async () => { events.push('browser.close') } }
  const cleanup = await performFailureCleanup(
    { page, browser, sessionId: 'session-under-test' },
    {
      endActiveSession: async (sessionId) => {
        events.push(`rest.end:${sessionId}`)
        return { ended: true, end_reason: 'technical_interruption' }
      },
      freezeTimeoutMs: 20,
    },
  )
  assert.deepEqual(events, [
    'freeze',
    'page.close',
    'browser.close',
    'rest.end:session-under-test',
  ])
  assert.equal(cleanup.frozen, true)
  assert.equal(cleanup.page_closed, true)
  assert.equal(cleanup.browser_closed, true)
  assert.equal(cleanup.session_end.ended, true)
}

function runStageCallLimitSelfTest() {
  const opening = [
    { model_role: 'actor', call_kind: 'initial', success: 1 },
    { model_role: 'tts', call_kind: 'initial', success: 1 },
  ]
  const counts = assertStageCallCounts(opening, { actorMax: 1, ttsMax: 1 })
  assert.equal(counts.actor.total, 1)
  assert.equal(counts.tts.total, 1)
  assert.throws(
    () => assertStageCallCounts([...opening, opening[0]], { actorMax: 1, ttsMax: 1 }),
    /actor 调用数超出当前阶段上限/,
  )
  assert.throws(
    () => assertStageCallCounts([
      ...opening,
      { model_role: 'report', call_kind: 'initial', success: 1 },
    ], { actorMax: 1, ttsMax: 1 }),
    /意外模型角色/,
  )
}

function runSocketTelemetryContractSelfTest() {
  const telemetry = createTelemetry()
  recordSocketCreated(telemetry, { requestId: 'socket-1', timestamp: 10 })
  assertNoAutomaticRetry(telemetry)
  assert.throws(
    () => assertNoAutomaticRetry(telemetry, { requireSessionStart: true }),
    /session\.start/,
  )

  recordSocketFrameSent(telemetry, {
    requestId: 'socket-1',
    timestamp: 10.125,
    response: { opcode: 1, payloadData: '{"type":"session.start"}' },
  })
  assert.doesNotThrow(
    () => assertNoAutomaticRetry(telemetry, { requireSessionStart: true }),
  )
  assert.deepEqual(publicTelemetry(telemetry).socket_lifecycle, [{
    socket_index: 1,
    created_monotonic_ms: 10_000,
    session_start_sent: 1,
    session_start_monotonic_ms: [10_125],
    closed: false,
    closed_monotonic_ms: null,
  }])

  recordSocketFrameSent(telemetry, {
    requestId: 'socket-1',
    timestamp: 10.25,
    response: { opcode: 1, payloadData: '{"type":"session.start"}' },
  })
  assert.throws(() => assertNoAutomaticRetry(telemetry), /session\.start/)

  const reconnect = createTelemetry()
  recordSocketCreated(reconnect, { requestId: 'socket-1', timestamp: 20 })
  recordSocketFrameSent(reconnect, {
    requestId: 'socket-1',
    timestamp: 20.1,
    response: { opcode: 1, payloadData: '{"type":"session.start"}' },
  })
  recordSocketCreated(reconnect, { requestId: 'socket-2', timestamp: 20.2 })
  assert.throws(() => assertNoAutomaticRetry(reconnect), /WebSocket 重连/)
}

async function runSelfTest() {
  assert.equal(parseMode([]), 'preflight')
  assert.equal(parseMode(['--execute-once']), 'execute-once')
  assert.throws(() => parseMode(['--execute-once', '--execute-once']))
  const result = summarizeEvidence({
    modelCalls: [
      { model_role: 'actor', call_kind: 'initial', success: 1 },
      { model_role: 'actor', call_kind: 'initial', success: 1 },
      { model_role: 'tts', call_kind: 'initial', success: 1 },
      { model_role: 'tts', call_kind: 'initial', success: 1 },
    ],
    turns: [{ speaker: 'worker', provider: 'asr', audio_path: 'worker.wav', text: '锁屏亮了一下' }],
    speechMetrics: [{ asr_sentences_json: '[{"text":"锁屏亮了一下"}]' }],
    failures: [],
    audioRecords: [{ kind: 'worker_turn' }],
  })
  assert.equal(result.call_counts.actor.total, 2)
  assert.equal(result.call_counts.tts.total, 2)
  assert.equal(result.asr_effective_turns, 1)
  assert.equal(result.asr_sentence_count, 1)
  assert.deepEqual(result.asr_transcript, ['锁屏亮了一下'])
  assert.equal(result.audio_record_counts.worker_turn, 1)
  assert.equal('masked_key' in result, false)
  assert.equal('workspace_id' in result, false)
  await runVirtualMicrophoneTimeoutSelfTest()
  await runVirtualMicrophoneDecodeTimeoutSelfTest()
  await runFailureFreezeSelfTest()
  await runFailureCleanupSelfTest()
  runStageCallLimitSelfTest()
  runSocketTelemetryContractSelfTest()
  printJson({ mode: 'self-test', passed: true, model_calls_made: 0 })
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    signal: AbortSignal.timeout(timeouts.fetch),
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  })
  if (options.expectedStatus !== undefined) {
    ensure(
      response.status === options.expectedStatus,
      `${url} 返回 HTTP ${response.status}，期望 ${options.expectedStatus}。`,
    )
  } else {
    ensure(response.ok, `${url} 返回 HTTP ${response.status}。`)
  }
  ensure(
    (response.headers.get('content-type') ?? '').includes('application/json'),
    `${url} 没有返回 JSON。`,
  )
  try {
    return await response.json()
  } catch {
    throw new AcceptanceError(`${url} 返回了无法解析的 JSON。`)
  }
}

async function checkFrontend() {
  const response = await fetch(`${frontendOrigin}/`, {
    signal: AbortSignal.timeout(timeouts.fetch),
    headers: { Accept: 'text/html' },
  })
  ensure(response.ok, `前端 ${frontendOrigin} 返回 HTTP ${response.status}。`)
  await response.body?.cancel()
  return { origin: frontendOrigin, ready: true }
}

function parseWavMetadata(buffer) {
  ensure(buffer.length >= 44, 'WAV 文件过短。')
  ensure(buffer.toString('ascii', 0, 4) === 'RIFF', 'WAV 缺少 RIFF 文件头。')
  ensure(buffer.toString('ascii', 8, 12) === 'WAVE', 'WAV 缺少 WAVE 标识。')
  let format = null
  let dataBytes = null
  let offset = 12
  while (offset + 8 <= buffer.length) {
    const chunkId = buffer.toString('ascii', offset, offset + 4)
    const chunkSize = buffer.readUInt32LE(offset + 4)
    const payloadOffset = offset + 8
    ensure(payloadOffset + chunkSize <= buffer.length, `WAV ${chunkId} 块长度越界。`)
    if (chunkId === 'fmt ') {
      ensure(chunkSize >= 16, 'WAV fmt 块过短。')
      format = {
        audio_format: buffer.readUInt16LE(payloadOffset),
        channels: buffer.readUInt16LE(payloadOffset + 2),
        sample_rate_hz: buffer.readUInt32LE(payloadOffset + 4),
        bits_per_sample: buffer.readUInt16LE(payloadOffset + 14),
      }
    }
    if (chunkId === 'data') dataBytes = chunkSize
    offset = payloadOffset + chunkSize + (chunkSize % 2)
  }
  ensure(format !== null, 'WAV 缺少 fmt 块。')
  ensure(dataBytes !== null && dataBytes > 0, 'WAV 缺少有效 data 块。')
  const bytesPerSecond = format.sample_rate_hz * format.channels * (format.bits_per_sample / 8)
  return {
    ...format,
    data_bytes: dataBytes,
    duration_seconds: Number((dataBytes / bytesPerSecond).toFixed(3)),
  }
}

function openReadOnlyDatabase() {
  return new DatabaseSync(databasePath, { readOnly: true })
}

function checkDatabase() {
  const db = openReadOnlyDatabase()
  try {
    const requiredTables = [
      'sessions', 'turns', 'model_call_metrics', 'runtime_failure_records',
      'speech_metric_records', 'audio_records',
    ]
    const available = new Set(
      db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all().map((row) => row.name),
    )
    const missing = requiredTables.filter((table) => !available.has(table))
    ensure(missing.length === 0, `验收数据库缺少表：${missing.join(', ')}。`)
    return { path: databasePath, ready: true, required_tables: requiredTables }
  } finally {
    db.close()
  }
}

function loadPlaywright() {
  const requireFromFrontend = createRequire(path.join(frontendDirectory, 'package.json'))
  return {
    playwright: requireFromFrontend('playwright'),
    packageJsonPath: requireFromFrontend.resolve('playwright/package.json'),
  }
}

function browserCandidates(chromium) {
  const candidates = []
  try {
    candidates.push(chromium.executablePath())
  } catch {
    // 本地未安装 Playwright 自带浏览器时，继续查找系统浏览器。
  }
  if (process.platform === 'win32') {
    const programFiles = process.env.ProgramFiles
    const programFilesX86 = process.env['ProgramFiles(x86)']
    const localAppData = process.env.LOCALAPPDATA
    if (programFiles) candidates.push(
      path.join(programFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.join(programFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    )
    if (programFilesX86) candidates.push(
      path.join(programFilesX86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.join(programFilesX86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    )
    if (localAppData) {
      candidates.push(path.join(localAppData, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    }
  } else if (process.platform === 'darwin') {
    candidates.push(
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    )
  } else {
    candidates.push('/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/microsoft-edge')
  }
  return [...new Set(candidates.filter(Boolean))]
}

function findBrowserExecutable(chromium) {
  const executable = browserCandidates(chromium).find((candidate) => existsSync(candidate))
  ensure(executable, '未找到可由 Playwright 启动的 Chrome/Edge/Chromium。')
  return executable
}

async function runPreflight() {
  const nodeMajor = Number(process.versions.node.split('.')[0])
  ensure(nodeMajor >= 24, '请使用 Node.js 24 或更高版本运行脚本。')
  const [wavBuffer, wavFile, dbFile, caseFile] = await Promise.all([
    readFile(wavPath), stat(wavPath), stat(databasePath), stat(casePath),
  ])
  ensure(wavFile.isFile(), 'WAV 路径不是文件。')
  ensure(dbFile.isFile() && dbFile.size > 0, '验收数据库不存在或为空。')
  ensure(caseFile.isFile(), `${caseId} 个案文件不存在。`)
  const wavSha256 = createHash('sha256').update(wavBuffer).digest('hex')
  ensure(wavSha256 === expectedWavSha256, 'WAV SHA-256 与验收基准不一致。')
  const wav = parseWavMetadata(wavBuffer)
  ensure(
    wav.audio_format === 1 && wav.channels === 1
      && wav.sample_rate_hz === 16_000 && wav.bits_per_sample === 16,
    'WAV 必须是 16 kHz、单声道、16-bit PCM。',
  )
  const { playwright, packageJsonPath } = loadPlaywright()
  const playwrightPackage = JSON.parse(await readFile(packageJsonPath, 'utf8'))
  const browserExecutable = findBrowserExecutable(playwright.chromium)
  const database = checkDatabase()
  const [frontend, health, providerConfig, demoConfig] = await Promise.all([
    checkFrontend(),
    fetchJson(`${backendOrigin}/api/health`),
    fetchJson(`${backendOrigin}/api/provider-config`),
    fetchJson(`${backendOrigin}/api/demo-config`),
  ])
  ensure(health.status === 'ready', '后端健康状态不是 ready。')
  ensure(providerConfig.configured === true, '真实验收所需模型凭证尚未配置。')
  ensure(
    demoConfig.scene === 'hotline' && demoConfig.case_type === 'main',
    '当前管理配置必须是 hotline/main，脚本不会自动改配置。',
  )
  return {
    publicResult: {
      mode: 'preflight',
      passed: true,
      would_execute: false,
      model_calls_made: 0,
      services: {
        frontend,
        backend: { origin: backendOrigin, status: health.status, service: health.service },
      },
      scenario: {
        mode: 'assessment', scene: demoConfig.scene, case_type: demoConfig.case_type,
        case_id: caseId, media: 'voice',
      },
      wav: { path: wavPath, sha256: wavSha256, ...wav },
      database,
      browser: {
        playwright_version: playwrightPackage.version,
        executable: browserExecutable,
        headed: true,
      },
      provider: {
        configured: true,
        actor_model: String(providerConfig.actor_model ?? ''),
        asr_model: String(providerConfig.asr_model ?? ''),
        tts_model: String(providerConfig.tts_model ?? ''),
        tts_voice: String(providerConfig.tts_voice ?? ''),
      },
      expected_billable_work: {
        actor_calls: 2,
        tts_calls: 2,
        actor_and_tts_calls_total: 4,
        asr_effective_turns: 1,
        asr_accounting_note: auditNotice,
        upstream_asr_websocket_connections: '不作为验收计数；本地数据库不持久化上游建连次数。',
      },
      safety: {
        execute_gate: '--execute-once', action_retries: 0, wav_injections: 0,
        session_created: false, browser_started: false,
      },
    },
    playwright,
    browserExecutable,
    wavBuffer,
  }
}

function readDatabaseEvidence(sessionId) {
  const db = openReadOnlyDatabase()
  try {
    const session = db.prepare(`
      SELECT id, mode, scene, case_type, case_id, media, status, model_mode,
             created_at, updated_at, ended_at, end_reason
      FROM sessions WHERE id = ?
    `).get(sessionId)
    const turns = db.prepare(`
      SELECT id, sequence, speaker, text, audio_path, provider, degraded, created_at
      FROM turns WHERE session_id = ? ORDER BY sequence
    `).all(sessionId)
    const modelCalls = db.prepare(`
      SELECT model_role, model_name, call_kind, cache_mode, latency_ms, success, created_at
      FROM model_call_metrics WHERE session_id = ? ORDER BY created_at, id
    `).all(sessionId)
    const failures = db.prepare(`
      SELECT component, phase, operation, failure_code, error_class, attempt_count,
             retryable, disposition, provider_status_code, created_at
      FROM runtime_failure_records WHERE session_id = ? ORDER BY created_at, id
    `).all(sessionId)
    const speechMetrics = db.prepare(`
      SELECT turn_id, first_response_ms, speech_duration_ms, asr_sentences_json, created_at
      FROM speech_metric_records WHERE session_id = ? ORDER BY created_at, id
    `).all(sessionId)
    const audioRecords = db.prepare(`
      SELECT kind, provider, size_bytes, created_at
      FROM audio_records WHERE session_id = ? ORDER BY created_at, id
    `).all(sessionId)
    return { session: session ?? null, turns, modelCalls, failures, speechMetrics, audioRecords }
  } finally {
    db.close()
  }
}

function assertDatabaseHealthy(
  sessionId,
  callLimits = { actorMax: Number.POSITIVE_INFINITY, ttsMax: Number.POSITIVE_INFINITY },
) {
  const evidence = readDatabaseEvidence(sessionId)
  ensure(evidence.session?.status === 'active', '会话在用户挂断前已结束，验收立即停止。')
  ensure(evidence.failures.length === 0, '数据库已记录运行失败，验收立即停止。')
  return {
    ...evidence,
    stageCallCounts: assertStageCallCounts(evidence.modelCalls, callLimits),
  }
}

async function endActiveSessionAfterFailure(sessionId) {
  const current = readDatabaseEvidence(sessionId).session
  if (!current) return { ended: false, reason: 'session_not_found' }
  if (current.status !== 'active') {
    return { ended: false, status: current.status, end_reason: current.end_reason }
  }
  const ended = await fetchJson(`${backendOrigin}/api/sessions/${sessionId}/end`, {
    method: 'POST',
    expectedStatus: 200,
    body: JSON.stringify({ reason: 'technical_interruption' }),
  })
  ensure(
    ended.id === sessionId
      && ended.status === 'ended'
      && ended.end_reason === 'technical_interruption',
    '失败清理未能把会话标记为 technical_interruption。',
  )
  return { ended: true, status: ended.status, end_reason: ended.end_reason }
}

async function settleWithin(promise, timeoutMs, message) {
  let timeoutId
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeoutId = setTimeout(() => reject(new AcceptanceError(message)), timeoutMs)
      }),
    ])
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId)
  }
}

async function performFailureCleanup(
  { page, browser, sessionId },
  {
    endActiveSession = endActiveSessionAfterFailure,
    freezeTimeoutMs = timeouts.failureFreeze,
  } = {},
) {
  const result = {
    frozen: false,
    page_closed: false,
    browser_closed: false,
    session_end: null,
    cleanup_errors: [],
  }
  if (page && !page.isClosed()) {
    try {
      const frozen = await settleWithin(
        page.evaluate(() => globalThis.__voiceAcceptance?.freeze()),
        freezeTimeoutMs,
        '页面失败冻结超时。',
      )
      ensure(frozen?.frozen === true, '页面未确认失败冻结。')
      result.frozen = true
    } catch (error) {
      result.cleanup_errors.push({ stage: 'freeze', ...printableError(error) })
    }
    try {
      await page.close({ runBeforeUnload: false })
      result.page_closed = true
    } catch (error) {
      result.cleanup_errors.push({ stage: 'page.close', ...printableError(error) })
    }
  }
  if (browser) {
    try {
      await browser.close()
      result.browser_closed = true
    } catch (error) {
      result.cleanup_errors.push({ stage: 'browser.close', ...printableError(error) })
    }
  }
  if (sessionId) {
    try {
      result.session_end = await endActiveSession(sessionId)
    } catch (error) {
      result.cleanup_errors.push({ stage: 'session.end', ...printableError(error) })
    }
  }
  return result
}

async function createExactSession() {
  const created = await fetchJson(`${backendOrigin}/api/sessions`, {
    method: 'POST',
    expectedStatus: 201,
    body: JSON.stringify({
      mode: 'assessment', scene: 'hotline', case_type: 'main', case_id: caseId,
    }),
  })
  ensure(typeof created.id === 'string' && created.id.length > 0, '创建会话未返回 session_id。')
  ensure(
    created.mode === 'assessment' && created.scene === 'hotline'
      && created.case_type === 'main' && created.case_id === caseId
      && created.media === 'voice' && created.status === 'active',
    '后端创建的会话与验收契约不一致。',
  )
  return created
}

function installVirtualMicrophone({ wavBase64, injectionTimeoutMs }) {
  const mediaDevices = navigator.mediaDevices
  if (!mediaDevices?.getUserMedia) throw new Error('浏览器不支持 getUserMedia')
  if (globalThis.__voiceAcceptance) throw new Error('虚拟麦克风被重复安装')
  if (!Number.isFinite(injectionTimeoutMs) || injectionTimeoutMs <= 0) {
    throw new Error('WAV 注入硬超时必须是正数')
  }
  const originalGetUserMedia = mediaDevices.getUserMedia.bind(mediaDevices)
  const NativeWebSocket = globalThis.WebSocket
  if (typeof NativeWebSocket !== 'function') throw new Error('浏览器不支持 WebSocket')
  const state = {
    microphones: [], injection_attempts: 0, injection_completed: 0,
    injection_started: false, active_source: null, active_cancel: null, sockets: new Set(),
    frozen: false, freeze_promise: null,
  }

  class AcceptanceWebSocket extends NativeWebSocket {
    constructor(...args) {
      if (state.frozen) throw new Error('验收已冻结，禁止新建 WebSocket')
      super(...args)
      state.sockets.add(this)
      this.addEventListener('close', () => state.sockets.delete(this), { once: true })
    }
  }
  globalThis.WebSocket = AcceptanceWebSocket

  Object.defineProperty(mediaDevices, 'getUserMedia', {
    configurable: true,
    value: async (constraints) => {
      if (state.frozen) throw new Error('验收已冻结，禁止重新打开麦克风')
      const wantsAudio = Boolean(constraints && typeof constraints === 'object' && constraints.audio)
      if (!wantsAudio) return originalGetUserMedia(constraints)
      const AudioContextConstructor = globalThis.AudioContext ?? globalThis.webkitAudioContext
      if (!AudioContextConstructor) throw new Error('浏览器不支持 AudioContext')
      const context = new AudioContextConstructor()
      const destination = context.createMediaStreamDestination()
      const keepAlive = context.createConstantSource()
      const silentGain = context.createGain()
      silentGain.gain.value = 0
      keepAlive.connect(silentGain)
      silentGain.connect(destination)
      keepAlive.start()
      await context.resume()
      state.microphones.push({ context, destination, keepAlive, silentGain })
      return destination.stream
    },
  })
  globalThis.__voiceAcceptance = Object.freeze({
    status: () => ({
      microphone_requests: state.microphones.length,
      live_microphones: state.microphones.filter((item) =>
        item.destination.stream.getAudioTracks().some((track) => track.readyState === 'live'),
      ).length,
      injection_attempts: state.injection_attempts,
      injection_completed: state.injection_completed,
      frozen: state.frozen,
      open_websockets: state.sockets.size,
    }),
    freeze: () => {
      if (state.freeze_promise) return state.freeze_promise
      state.frozen = true
      state.freeze_promise = (async () => {
        if (state.active_cancel) {
          state.active_cancel(new Error('验收失败已冻结'))
        } else if (state.active_source) {
          state.active_source.onended = null
          try {
            state.active_source.stop()
          } catch {
            // 源已停止时无需再处理。
          }
          state.active_source.disconnect()
          state.active_source = null
        }
        for (const socket of state.sockets) {
          socket.onopen = null
          socket.onmessage = null
          socket.onclose = null
          socket.onerror = null
          try {
            socket.close()
          } catch {
            // 连接已关闭时无需再处理。
          }
        }
        state.sockets.clear()
        const closingContexts = state.microphones.map((microphone) => {
          try {
            microphone.keepAlive.stop()
          } catch {
            // 保活音源已停止时无需再处理。
          }
          microphone.keepAlive.disconnect()
          microphone.silentGain.disconnect()
          for (const track of microphone.destination.stream.getTracks()) track.stop()
          try {
            return Promise.resolve(microphone.context.close())
              .catch(() => microphone.context.suspend().catch(() => undefined))
          } catch {
            return microphone.context.suspend().catch(() => undefined)
          }
        })
        await Promise.allSettled(closingContexts)
        return { ...globalThis.__voiceAcceptance.status(), frozen: true }
      })()
      return state.freeze_promise
    },
    playOnce: async () => {
      if (state.frozen) throw new Error('验收已冻结，禁止 WAV 注入')
      if (state.injection_started) throw new Error('WAV 注入已尝试，禁止重复执行')
      state.injection_started = true
      state.injection_attempts += 1
      const microphone = [...state.microphones].reverse().find((item) =>
        item.destination.stream.getAudioTracks().some((track) => track.readyState === 'live'),
      )
      if (!microphone) throw new Error('没有找到活动的虚拟麦克风')
      return await new Promise((resolve, reject) => {
        let settled = false
        let timeoutId
        let source = null
        const stopSource = () => {
          if (!source) return
          source.onended = null
          try {
            source.stop()
          } catch {
            // 源已停止时无需再处理。
          }
          source.disconnect()
          source = null
          state.active_source = null
        }
        const closeMicrophone = () => {
          try {
            microphone.keepAlive.stop()
          } catch {
            // 保活音源已停止时无需再处理。
          }
          microphone.keepAlive.disconnect()
          microphone.silentGain.disconnect()
          for (const track of microphone.destination.stream.getTracks()) track.stop()
          try {
            void Promise.resolve(microphone.context.close())
              .catch(() => microphone.context.suspend().catch(() => undefined))
          } catch {
            try {
              void microphone.context.suspend().catch(() => undefined)
            } catch {
              // AudioContext 已无法继续操作。
            }
          }
        }
        const cancel = (error, { closeContext = false } = {}) => {
          if (settled) return
          settled = true
          if (timeoutId !== undefined) globalThis.clearTimeout(timeoutId)
          stopSource()
          state.active_cancel = null
          if (closeContext) closeMicrophone()
          reject(error)
        }
        state.active_cancel = cancel
        timeoutId = globalThis.setTimeout(() => {
          if (settled) return
          cancel(
            new Error(`WAV 注入超时（${injectionTimeoutMs}ms）`),
            { closeContext: true },
          )
        }, injectionTimeoutMs)
        void (async () => {
          await microphone.context.resume()
          if (settled) return
          const binary = atob(wavBase64)
          const bytes = new Uint8Array(binary.length)
          for (let index = 0; index < binary.length; index += 1) {
            bytes[index] = binary.charCodeAt(index)
          }
          const decoded = await microphone.context.decodeAudioData(bytes.buffer)
          if (settled) return
          source = microphone.context.createBufferSource()
          source.buffer = decoded
          source.loop = false
          source.connect(microphone.destination)
          state.active_source = source
          source.onended = () => {
            if (settled) return
            settled = true
            globalThis.clearTimeout(timeoutId)
            source.disconnect()
            source = null
            state.active_source = null
            state.active_cancel = null
            state.injection_completed += 1
            resolve({ duration_seconds: decoded.duration, ...globalThis.__voiceAcceptance.status() })
          }
          source.start(0)
        })().catch((error) => cancel(error))
      })
    },
  })
}

function createTelemetry() {
  return {
    socket_request_ids: new Set(), socket_connections_observed: 0, websocket_closed: 0,
    socket_records_by_request_id: new Map(), session_start_sent: 0,
    visitor_text_events: 0, turn_committed_events: 0, playback_ended_sent: 0,
    manual_complete_sent: 0, client_failure_sent: 0,
    input_error_events: 0, technical_pause_events: 0,
    phases: [], tts_binary_chunks_by_visitor: [], unassigned_tts_binary_chunks: 0,
  }
}

function monotonicMilliseconds(timestamp) {
  return Number.isFinite(timestamp) ? Number((timestamp * 1_000).toFixed(3)) : null
}

function parseWebSocketFrame(frame) {
  if (frame.opcode !== 1) return null
  try {
    return JSON.parse(frame.payloadData)
  } catch {
    return null
  }
}

function recordSocketCreated(telemetry, event) {
  if (telemetry.socket_records_by_request_id.has(event.requestId)) return
  telemetry.socket_request_ids.add(event.requestId)
  telemetry.socket_connections_observed += 1
  telemetry.socket_records_by_request_id.set(event.requestId, {
    socket_index: telemetry.socket_connections_observed,
    created_monotonic_ms: monotonicMilliseconds(event.timestamp),
    session_start_sent: 0,
    session_start_monotonic_ms: [],
    closed: false,
    closed_monotonic_ms: null,
  })
}

function recordSocketClosed(telemetry, event) {
  const record = telemetry.socket_records_by_request_id.get(event.requestId)
  if (!record || record.closed) return
  record.closed = true
  record.closed_monotonic_ms = monotonicMilliseconds(event.timestamp)
  telemetry.websocket_closed += 1
}

function recordSocketFrameSent(telemetry, event) {
  const record = telemetry.socket_records_by_request_id.get(event.requestId)
  if (!record) return ''
  const type = String(parseWebSocketFrame(event.response)?.type ?? '')
  if (type === 'session.start') {
    telemetry.session_start_sent += 1
    record.session_start_sent += 1
    record.session_start_monotonic_ms.push(monotonicMilliseconds(event.timestamp))
  }
  return type
}

async function attachTelemetry(page, sessionId) {
  const cdp = await page.context().newCDPSession(page)
  const telemetry = createTelemetry()
  const matchesSession = (url) => url.includes(`/api/live-sessions/${sessionId}`)
  cdp.on('Network.webSocketCreated', (event) => {
    if (!matchesSession(event.url)) return
    recordSocketCreated(telemetry, event)
  })
  cdp.on('Network.webSocketClosed', (event) => {
    recordSocketClosed(telemetry, event)
  })
  cdp.on('Network.webSocketFrameReceived', (event) => {
    if (!telemetry.socket_request_ids.has(event.requestId)) return
    if (event.response.opcode === 2) {
      const group = telemetry.visitor_text_events - 1
      if (group < 0) telemetry.unassigned_tts_binary_chunks += 1
      else telemetry.tts_binary_chunks_by_visitor[group] =
        (telemetry.tts_binary_chunks_by_visitor[group] ?? 0) + 1
      return
    }
    const message = parseWebSocketFrame(event.response)
    const type = String(message?.type ?? '')
    if (type === 'visitor.text') {
      telemetry.visitor_text_events += 1
      telemetry.tts_binary_chunks_by_visitor.push(0)
    } else if (type === 'turn.committed') telemetry.turn_committed_events += 1
    else if (type === 'phase') telemetry.phases.push(String(message.phase ?? ''))
    else if (type === 'input.error') telemetry.input_error_events += 1
    else if (type === 'technical.pause') telemetry.technical_pause_events += 1
  })
  cdp.on('Network.webSocketFrameSent', (event) => {
    if (!telemetry.socket_request_ids.has(event.requestId)) return
    const type = recordSocketFrameSent(telemetry, event)
    if (type === 'playback.ended') telemetry.playback_ended_sent += 1
    if (type === 'turn.manual_complete') telemetry.manual_complete_sent += 1
    if (type === 'client.failure') telemetry.client_failure_sent += 1
  })
  await cdp.send('Network.enable')
  return { cdp, telemetry }
}

function assertNoAutomaticRetry(telemetry, { requireSessionStart = false } = {}) {
  ensure(
    telemetry.socket_connections_observed <= 1,
    '检测到 WebSocket 重连，为避免非单次执行，验收立即停止。',
  )
  ensure(
    telemetry.session_start_sent <= 1,
    '检测到重复的 session.start，验收立即停止。',
  )
  ensure(
    [...telemetry.socket_records_by_request_id.values()].every(
      (record) => record.session_start_sent <= 1,
    ),
    '同一条 WebSocket 发送了重复的 session.start，验收立即停止。',
  )
  if (requireSessionStart) {
    ensure(
      telemetry.socket_connections_observed === 1 && telemetry.session_start_sent === 1,
      '尚未观测到唯一业务 WebSocket 的 session.start。',
    )
  }
  ensure(telemetry.websocket_closed === 0, 'WebSocket 提前关闭，验收立即停止。')
  ensure(telemetry.manual_complete_sent <= 1, '检测到重复的 turn.manual_complete。')
  ensure(telemetry.visitor_text_events <= 2, '检测到超出单轮验收的 visitor.text。')
  ensure(telemetry.playback_ended_sent <= 2, '检测到超出单轮验收的 playback.ended。')
  ensure(telemetry.client_failure_sent === 0, '浏览器已上报 client.failure，验收立即停止。')
  ensure(telemetry.input_error_events === 0, '收到 input.error，验收立即停止。')
  ensure(telemetry.technical_pause_events === 0, '会话进入技术中断，验收立即停止。')
  ensure(
    !telemetry.phases.includes('technical_paused'),
    '会话 phase 进入 technical_paused，验收立即停止。',
  )
}

function publicTelemetry(telemetry) {
  return {
    socket_connections_observed: telemetry.socket_connections_observed,
    session_start_sent: telemetry.session_start_sent,
    socket_lifecycle: [...telemetry.socket_records_by_request_id.values()].map((record) => ({
      socket_index: record.socket_index,
      created_monotonic_ms: record.created_monotonic_ms,
      session_start_sent: record.session_start_sent,
      session_start_monotonic_ms: [...record.session_start_monotonic_ms],
      closed: record.closed,
      closed_monotonic_ms: record.closed_monotonic_ms,
    })),
    websocket_closed: telemetry.websocket_closed,
    visitor_text_events: telemetry.visitor_text_events,
    turn_committed_events: telemetry.turn_committed_events,
    playback_ended_sent: telemetry.playback_ended_sent,
    manual_complete_sent: telemetry.manual_complete_sent,
    client_failure_sent: telemetry.client_failure_sent,
    input_error_events: telemetry.input_error_events,
    technical_pause_events: telemetry.technical_pause_events,
    phases: [...telemetry.phases],
    tts_binary_chunks_by_visitor: [...telemetry.tts_binary_chunks_by_visitor],
    unassigned_tts_binary_chunks: telemetry.unassigned_tts_binary_chunks,
  }
}

async function waitForCondition(label, timeoutMs, probe) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const result = await probe()
    if (result) return result
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new AcceptanceError(`等待“${label}”超时；脚本不会重试该步骤。`)
}

async function manualButtonReady(page) {
  const button = page.getByRole('button', { name: '我说完了', exact: true })
  if (await button.count() !== 1) return false
  return await button.isVisible() && await button.isEnabled()
}

function assertPageAndTelemetry(page, telemetry, options) {
  ensure(!page.isClosed(), '浏览器页面已关闭。')
  assertNoAutomaticRetry(telemetry, options)
}

async function waitForManualEnd(page, sessionId, telemetry) {
  return await waitForCondition('用户手动挂断', timeouts.manualEnd, async () => {
    const evidence = readDatabaseEvidence(sessionId)
    if (evidence.session?.status === 'ended') return evidence.session.end_reason ?? 'ended'
    assertPageAndTelemetry(page, telemetry, { requireSessionStart: true })
    ensure(evidence.failures.length === 0, '数据库已记录运行失败，验收立即停止。')
    assertStageCallCounts(evidence.modelCalls, { actorMax: 2, ttsMax: 2 })
    ensure(!page.isClosed(), '浏览器已关闭，但数据库会话仍是 active。')
    return false
  })
}

function buildAcceptanceResult({ sessionId, rawEvidence, telemetry, runState }) {
  const evidence = summarizeEvidence(rawEvidence)
  const publicCdp = publicTelemetry(telemetry)
  const firstThreeSpeakers = evidence.transcript_speakers.slice(0, 3)
  const checks = {
    session_ended_by_user:
      evidence.session?.status === 'ended' && evidence.session?.end_reason === 'user_ended',
    exact_three_turns:
      evidence.transcript_speakers.length === 3
      && JSON.stringify(firstThreeSpeakers) === JSON.stringify(['client', 'worker', 'client']),
    actor_calls_exactly_two:
      evidence.call_counts.actor.total === 2
      && evidence.call_counts.actor.success === 2
      && evidence.call_counts.actor.repair === 0,
    tts_calls_exactly_two:
      evidence.call_counts.tts.total === 2
      && evidence.call_counts.tts.success === 2
      && evidence.call_counts.tts.repair === 0,
    asr_effective_turns_exactly_one: evidence.asr_effective_turns === 1,
    no_unexpected_model_roles: evidence.unexpected_model_call_roles.length === 0,
    no_runtime_failures: evidence.failure_records.length === 0,
    one_browser_websocket_without_reconnect: publicCdp.socket_connections_observed === 1,
    session_start_sent_exactly_once:
      publicCdp.session_start_sent === 1
      && publicCdp.socket_lifecycle.length === 1
      && publicCdp.socket_lifecycle[0].session_start_sent === 1,
    no_browser_runtime_failures:
      publicCdp.input_error_events === 0
      && publicCdp.technical_pause_events === 0
      && publicCdp.client_failure_sent === 0
      && !publicCdp.phases.includes('technical_paused'),
    wav_injected_once:
      runState.injection_attempts === 1 && runState.injection_completed === 1,
    manual_complete_once:
      runState.manual_complete_clicks === 1 && publicCdp.manual_complete_sent === 1,
    opening_and_reply_text_seen: publicCdp.visitor_text_events === 2,
    opening_and_reply_committed: publicCdp.turn_committed_events === 2,
    opening_and_reply_tts_seen:
      publicCdp.tts_binary_chunks_by_visitor.length === 2
      && publicCdp.tts_binary_chunks_by_visitor.every((count) => count > 0)
      && publicCdp.unassigned_tts_binary_chunks === 0,
    opening_and_reply_played: publicCdp.playback_ended_sent === 2,
  }
  return {
    mode: 'execute-once',
    session_id: sessionId,
    acceptance_passed: Object.values(checks).every(Boolean),
    checks,
    ...evidence,
    browser_observation: publicCdp,
    script_actions: { ...runState },
  }
}

async function executeOnce(preflight) {
  ensure(
    process.stdin.isTTY && process.stdout.isTTY,
    '--execute-once 必须在可交互终端中运行，以便最后由用户手动挂断。',
  )
  const { chromium } = preflight.playwright
  const runState = {
    start_check_clicks: 0, answer_clicks: 0, injection_attempts: 0,
    injection_completed: 0, manual_complete_clicks: 0,
    automatic_retries: 0, automatic_hangups: 0,
  }
  let browser = null
  let page = null
  let sessionId = null
  let paidPathStarted = false
  let telemetry = null
  let finalEvidencePrinted = false
  try {
    browser = await chromium.launch({
      headless: false,
      executablePath: preflight.browserExecutable,
      args: ['--autoplay-policy=no-user-gesture-required'],
      timeout: timeouts.navigation,
    })
    const context = await browser.newContext({
      permissions: ['microphone'],
      viewport: { width: 1440, height: 960 },
    })
    await context.addInitScript(installVirtualMicrophone, {
      wavBase64: preflight.wavBuffer.toString('base64'),
      injectionTimeoutMs: timeouts.wavInjection,
    })
    page = await context.newPage()
    page.setDefaultTimeout(timeouts.browserAction)
    page.setDefaultNavigationTimeout(timeouts.navigation)

    const created = await createExactSession()
    sessionId = created.id
    printJson({
      event: 'session_created', session_id: sessionId,
      model_calls_made_so_far: 0, paid_path_started: false,
    })
    const initialEvidence = readDatabaseEvidence(sessionId)
    ensure(initialEvidence.session?.id === sessionId, '活动数据库中找不到新会话。')
    ensure(initialEvidence.session.status === 'active', '新会话在数据库中不是 active。')
    ensure(initialEvidence.modelCalls.length === 0, '进入会话前已出现模型调用记录。')

    telemetry = (await attachTelemetry(page, sessionId)).telemetry
    const query = new URLSearchParams({
      mode: 'assessment', scene: 'hotline', caseType: 'main', sessionId,
    })
    await page.goto(`${frontendOrigin}/device-check?${query}`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: '开始检查', exact: true }).click()
    runState.start_check_clicks = 1
    const answer = page.getByRole('button', { name: '接听来电', exact: true })
    await waitForCondition('三项设备检查就绪', timeouts.readiness, async () => {
      if (await page.locator('.readiness-item--failed').count() > 0) {
        throw new AcceptanceError('设备检查失败，脚本不会点击“重新检查”。')
      }
      return await page.locator('.readiness-item--passed').count() === 3
        && await answer.isEnabled()
    })

    await answer.click()
    paidPathStarted = true
    runState.answer_clicks = 1
    await page.waitForURL(new RegExp(`/session/${sessionId}(?:\\?|$)`))
    await waitForCondition('开场文字与 TTS 完整播放', timeouts.openingRound, async () => {
      assertPageAndTelemetry(page, telemetry)
      const current = assertDatabaseHealthy(sessionId, { actorMax: 1, ttsMax: 1 })
      ensure(current.turns.length <= 1, '开场阶段意外出现多个话轮。')
      const openingCommitted = current.turns.length === 1
        && current.turns[0]?.speaker === 'client'
        && Boolean(String(current.turns[0]?.text ?? '').trim())
      return openingCommitted
        && telemetry.session_start_sent === 1
        && telemetry.visitor_text_events === 1
        && (telemetry.tts_binary_chunks_by_visitor[0] ?? 0) > 0
        && telemetry.playback_ended_sent === 1
        && current.stageCallCounts.actor.total === 1
        && current.stageCallCounts.tts.total === 1
        && await manualButtonReady(page)
        && await page.getByText('麦克风已连接', { exact: true }).isVisible()
    })

    runState.injection_attempts = 1
    const injection = await page.evaluate(() => globalThis.__voiceAcceptance.playOnce())
    ensure(injection.injection_attempts === 1, 'WAV 注入尝试次数不是 1。')
    ensure(injection.injection_completed === 1, 'WAV 未完整播入虚拟麦克风。')
    runState.injection_completed = 1
    const provisional = await waitForCondition('ASR 临时转写', timeouts.asrTranscript, async () => {
      assertPageAndTelemetry(page, telemetry, { requireSessionStart: true })
      assertDatabaseHealthy(sessionId, { actorMax: 1, ttsMax: 1 })
      const locator = page.locator('.live-turn--worker.live-turn--provisional p')
      if (await locator.count() !== 1) return false
      return String(await locator.textContent() ?? '').trim() || false
    })

    const manualComplete = page.getByRole('button', { name: '我说完了', exact: true })
    ensure(await manualComplete.isEnabled(), '“我说完了”不可点击。')
    await manualComplete.click()
    runState.manual_complete_clicks = 1
    await waitForCondition('回复文字与 TTS 完整播放', timeouts.replyRound, async () => {
      assertPageAndTelemetry(page, telemetry, { requireSessionStart: true })
      const current = assertDatabaseHealthy(sessionId, { actorMax: 2, ttsMax: 2 })
      const speakers = current.turns.map((turn) => turn.speaker)
      ensure(speakers.length <= 3, '单轮验收意外出现超过三个已提交话轮。')
      return speakers.length === 3
        && JSON.stringify(speakers) === JSON.stringify(['client', 'worker', 'client'])
        && telemetry.visitor_text_events === 2
        && (telemetry.tts_binary_chunks_by_visitor[1] ?? 0) > 0
        && telemetry.playback_ended_sent === 2
        && telemetry.manual_complete_sent === 1
        && current.stageCallCounts.actor.total === 2
        && current.stageCallCounts.tts.total === 2
        && await manualButtonReady(page)
    })

    const interim = summarizeEvidence(readDatabaseEvidence(sessionId))
    printJson({
      event: 'round_trip_ready_for_manual_end',
      session_id: sessionId,
      provisional_asr_transcript: provisional,
      committed_asr_transcript: interim.asr_transcript,
      call_counts: interim.call_counts,
      asr_effective_turns: interim.asr_effective_turns,
      asr_effective_turn_definition: auditNotice,
      failure_records: interim.failure_records,
      instruction: '请在浏览器中点击“立即挂断并结束”，再点击“确认立即挂断”。脚本不会代替你点击。',
    })
    await waitForManualEnd(page, sessionId, telemetry)
    const result = buildAcceptanceResult({
      sessionId, rawEvidence: readDatabaseEvidence(sessionId), telemetry, runState,
    })
    printJson(result)
    finalEvidencePrinted = true
    if (!result.acceptance_passed) process.exitCode = 1
  } catch (error) {
    const cleanup = await performFailureCleanup({ page, browser, sessionId })
    page = null
    browser = null
    console.error(JSON.stringify({
      event: 'acceptance_step_failed', session_id: sessionId,
      paid_path_started: paidPathStarted, action_retries: 0,
      cleanup,
      error: printableError(error),
    }, null, 2))
    if (sessionId && !finalEvidencePrinted) {
      try {
        const evidence = summarizeEvidence(readDatabaseEvidence(sessionId))
        printJson({
          event: 'final_evidence_after_failure', session_id: sessionId, ...evidence,
          browser_observation: telemetry ? publicTelemetry(telemetry) : null,
          script_actions: { ...runState },
          cleanup,
        })
      } catch (evidenceError) {
        console.error(JSON.stringify({
          event: 'failure_evidence_unavailable', session_id: sessionId,
          error: printableError(evidenceError),
        }, null, 2))
      }
    }
    throw error
  } finally {
    await browser?.close().catch(() => undefined)
  }
}

async function main() {
  const mode = parseMode(process.argv.slice(2))
  if (mode === 'help') return printUsage()
  if (mode === 'self-test') return await runSelfTest()
  const preflight = await runPreflight()
  printJson(preflight.publicResult)
  if (mode === 'execute-once') await executeOnce(preflight)
}

await main().catch((error) => {
  console.error(JSON.stringify({
    event: 'acceptance_script_failed', model_action_retries: 0,
    error: printableError(error),
  }, null, 2))
  process.exitCode = 1
})
