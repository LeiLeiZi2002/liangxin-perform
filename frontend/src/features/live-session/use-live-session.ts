import { useCallback, useEffect, useRef, useState } from 'react'

import { endSession as endSessionRequest } from '../../api/client'
import type { RuntimePhase } from '../../api/contracts'
import {
  LiveAudioCapture,
  LiveAudioCaptureError,
  type LiveAudioCaptureCallbacks,
  type LiveAudioCaptureErrorCode,
} from '../live-audio/audio-capture'
import { PcmAudioPlayback } from '../live-audio/audio-playback'

export type LiveTurn = {
  id: string
  sequence: number
  speaker: 'worker' | 'client'
  text: string
  client_turn_id: string
}

export type TechnicalPause = {
  message: string
  canRetry: boolean
}

export type VoiceActivity = {
  state: 'quiet' | 'speaking' | 'paused'
  confirmedSilenceMs: number
}

export type TextTurnStatus = 'idle' | 'pending' | 'committed' | 'failed'

export type VisitorReveal = {
  turnId: string | null
  visibleSegments: string[]
  isTyping: boolean
}

type CaptureController = {
  start: () => Promise<void>
  close: () => Promise<void> | void
}

type PlaybackController = {
  queue: (pcm: ArrayBuffer) => Promise<void>
  stop: () => void
  close: () => Promise<void>
}

export type LiveSessionDependencies = {
  createSocket?: (url: string) => WebSocket
  createCapture?: (callbacks: LiveAudioCaptureCallbacks) => CaptureController
  createPlayback?: (onIdle: () => void) => PlaybackController
  reconnectDelayMs?: number
  textMessageIntervalMs?: number
}

const defaultDependencies: LiveSessionDependencies = {}

type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'closed'
type EndRequestReason = 'user_ended' | 'technical_interruption'
type ClientFailureStage = 'capture' | 'playback'
type ClientFailure = {
  type: 'client.failure'
  stage: ClientFailureStage
  code: string
}
type EndRestOperation = {
  sessionId: string
  promise: Promise<void>
}

const END_ACK_TIMEOUT_RECONNECT_MULTIPLIER = 2
const makeId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`

export function liveSessionSocketUrl(sessionId: string) {
  const configuredBase = (import.meta.env.VITE_API_BASE_URL ?? '')
    .replace(/\/$/, '')
    .replace(/\/api$/, '')
  const base = configuredBase || window.location.origin
  const url = new URL(`${base}/api/live-sessions/${encodeURIComponent(sessionId)}`)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

function mergeTurns(existing: LiveTurn[], received: Array<LiveTurn | undefined>) {
  const byId = new Map(existing.map((turn) => [turn.id, turn]))
  for (const turn of received) if (turn) byId.set(turn.id, turn)
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence)
}

export function splitOnlineMessages(text: string) {
  return text
    .split(/\r?\n/)
    .map((segment) => segment.trim())
    .filter(Boolean)
}

function detachSocket(socket: WebSocket) {
  socket.onopen = null
  socket.onmessage = null
  socket.onclose = null
  socket.onerror = null
}

export function useLiveSession(
  sessionId: string,
  media: 'voice' | 'text',
  dependencies: LiveSessionDependencies = defaultDependencies,
) {
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [phase, setPhase] = useState<RuntimePhase>('listening')
  const [transcript, setTranscript] = useState<LiveTurn[]>([])
  const [liveTranscript, setLiveTranscript] = useState('')
  const [visitorPreview, setVisitorPreview] = useState('')
  const [visitorReveal, setVisitorReveal] = useState<VisitorReveal | null>(null)
  const [textTurnStatus, setTextTurnStatus] = useState<TextTurnStatus>('idle')
  const [technicalPause, setTechnicalPause] = useState<TechnicalPause | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [inputError, setInputError] = useState('')
  const [inputNotice, setInputNotice] = useState('')
  const [isPlaying, setIsPlaying] = useState(false)
  const [endedReason, setEndedReason] = useState<string | null>(null)
  const [energy, setEnergy] = useState(0)
  const [voiceActivity, setVoiceActivity] = useState<VoiceActivity>({
    state: 'quiet',
    confirmedSilenceMs: 0,
  })
  const [manualCompletePending, setManualCompletePending] = useState(false)
  const [redoInputPending, setRedoInputPending] = useState(false)
  const [canRedoInputCapability, setCanRedoInputCapability] = useState(true)

  const socketRef = useRef<WebSocket | null>(null)
  const captureRef = useRef<CaptureController | null>(null)
  const playbackRef = useRef<PlaybackController | null>(null)
  const phaseRef = useRef<RuntimePhase>('listening')
  const reconnectTimerRef = useRef<number | null>(null)
  const endAckTimerRef = useRef<number | null>(null)
  const endRestOperationRef = useRef<EndRestOperation | null>(null)
  const disposedRef = useRef(false)
  const activeSessionIdRef = useRef(sessionId)
  const reconnectAllowedRef = useRef(true)
  const sendAudioRef = useRef(true)
  const replyCommittedRef = useRef(false)
  const replyHasAudioRef = useRef(false)
  const playbackEndedSentRef = useRef(false)
  const playbackActiveRef = useRef(false)
  const technicalPausedRef = useRef(false)
  const retryRequestedRef = useRef(false)
  const captureNeedsRestartRef = useRef(false)
  const endingRef = useRef(false)
  const pendingEndReasonRef = useRef<EndRequestReason | null>(null)
  const endConfirmedRef = useRef(false)
  const pendingClientFailuresRef = useRef<ClientFailure[]>([])
  const manualCompletePendingRef = useRef(false)
  const redoInputPendingRef = useRef(false)
  const liveTranscriptRef = useRef('')
  const textTurnPendingRef = useRef(false)
  const pendingTextTurnIdRef = useRef<string | null>(null)
  const visitorRevealTextRef = useRef('')
  const visitorRevealTimerRef = useRef<number | null>(null)
  const reconnectDelayMs = dependencies.reconnectDelayMs ?? 900
  const textMessageIntervalMs = dependencies.textMessageIntervalMs ?? 360
  const endAckTimeoutMs = reconnectDelayMs * END_ACK_TIMEOUT_RECONNECT_MULTIPLIER

  const sendJson = useCallback((event: Record<string, unknown>) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify(event))
      return true
    }
    return false
  }, [])

  const clearVisitorRevealTimer = useCallback(() => {
    if (visitorRevealTimerRef.current === null) return
    window.clearTimeout(visitorRevealTimerRef.current)
    visitorRevealTimerRef.current = null
  }, [])

  const finishPlayback = useCallback(() => {
    playbackActiveRef.current = false
    setIsPlaying(false)
    if (
      replyCommittedRef.current
      && replyHasAudioRef.current
      && !playbackEndedSentRef.current
    ) {
      playbackEndedSentRef.current = true
      sendJson({ type: 'playback.ended' })
    }
  }, [sendJson])

  const reportClientFailure = useCallback((stage: ClientFailureStage, code: string) => {
    const event: ClientFailure = { type: 'client.failure', stage, code }
    if (!sendJson(event)) pendingClientFailuresRef.current.push(event)
  }, [sendJson])

  const stopMedia = useCallback(() => {
    sendAudioRef.current = false
    playbackActiveRef.current = false
    setIsPlaying(false)
    playbackRef.current?.stop()
    void playbackRef.current?.close()
    void captureRef.current?.close()
  }, [])

  const clearEndAckTimer = useCallback(() => {
    if (endAckTimerRef.current === null) return
    window.clearTimeout(endAckTimerRef.current)
    endAckTimerRef.current = null
  }, [])

  const completeEnded = useCallback((reason: string) => {
    endConfirmedRef.current = true
    endingRef.current = true
    pendingEndReasonRef.current = null
    reconnectAllowedRef.current = false
    clearEndAckTimer()
    if (disposedRef.current) return
    stopMedia()
    const socket = socketRef.current
    if (socket) {
      socketRef.current = null
      detachSocket(socket)
      socket.close()
    }
    setInputError('')
    setInputNotice('')
    setTextTurnStatus('idle')
    textTurnPendingRef.current = false
    pendingTextTurnIdRef.current = null
    clearVisitorRevealTimer()
    visitorRevealTextRef.current = ''
    setVisitorReveal(null)
    setManualCompletePending(false)
    manualCompletePendingRef.current = false
    setRedoInputPending(false)
    redoInputPendingRef.current = false
    setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
    setEndedReason(reason)
    phaseRef.current = 'ended'
    setPhase('ended')
    setConnection('closed')
  }, [clearEndAckTimer, clearVisitorRevealTimer, stopMedia])

  const settleEndViaRest = useCallback((reason: EndRequestReason) => {
    const requestSessionId = sessionId
    if (
      activeSessionIdRef.current === requestSessionId
      && endConfirmedRef.current
    ) return Promise.resolve()
    const existing = endRestOperationRef.current
    if (existing?.sessionId === requestSessionId) return existing.promise

    const request = endSessionRequest(requestSessionId, reason)
      .then((ended) => {
        if (activeSessionIdRef.current !== requestSessionId) return
        if (endConfirmedRef.current) return
        if (!ended.end_reason) throw new Error('结束接口没有返回结束原因')
        completeEnded(ended.end_reason)
      })
      .catch(() => {
        if (
          activeSessionIdRef.current !== requestSessionId
          || endConfirmedRef.current
          || disposedRef.current
        ) return
        endingRef.current = false
        pendingEndReasonRef.current = null
        setInputError('结束会谈没有完成，请重新连接后再试。')
      })
      .finally(() => {
        if (endRestOperationRef.current?.promise === request) {
          endRestOperationRef.current = null
        }
      })
    endRestOperationRef.current = { sessionId: requestSessionId, promise: request }
    return request
  }, [completeEnded, sessionId])

  const armEndAckTimer = useCallback((reason: EndRequestReason) => {
    clearEndAckTimer()
    const timer = window.setTimeout(() => {
      if (endAckTimerRef.current !== timer) return
      endAckTimerRef.current = null
      if (
        disposedRef.current
        || activeSessionIdRef.current !== sessionId
        || endConfirmedRef.current
      ) return
      void settleEndViaRest(reason)
    }, endAckTimeoutMs)
    endAckTimerRef.current = timer
  }, [clearEndAckTimer, endAckTimeoutMs, sessionId, settleEndViaRest])

  const showCaptureFailure = useCallback((error: unknown) => {
    const code: LiveAudioCaptureErrorCode = error instanceof LiveAudioCaptureError
      ? error.code
      : 'capture_unavailable'
    const message = code === 'permission_denied'
      ? '这边暂时听不到你的声音，请允许浏览器使用麦克风后重试。'
      : code === 'microphone_ended'
        ? '这边的声音刚刚断开了，请检查麦克风连接后重试。'
        : '这边暂时听不到你的声音，请检查麦克风后重试。'

    reportClientFailure('capture', code)
    retryRequestedRef.current = false
    captureNeedsRestartRef.current = true
    sendAudioRef.current = false
    redoInputPendingRef.current = false
    technicalPausedRef.current = true
    setRetrying(false)
    setRedoInputPending(false)
    setInputNotice('')
    void captureRef.current?.close()
    setTechnicalPause({ message, canRetry: true })
    phaseRef.current = 'technical_paused'
    setPhase('technical_paused')
  }, [reportClientFailure])

  const showPlaybackFailure = useCallback(() => {
    reportClientFailure('playback', 'playback_failed')
    retryRequestedRef.current = false
    sendAudioRef.current = false
    redoInputPendingRef.current = false
    playbackActiveRef.current = false
    technicalPausedRef.current = true
    setRetrying(false)
    setRedoInputPending(false)
    setInputNotice('')
    playbackRef.current?.stop()
    setIsPlaying(false)
    setTechnicalPause({
      message: '来访者的声音没有正常播放，本次会谈需要先停下来确认。',
      canRetry: true,
    })
    phaseRef.current = 'technical_paused'
    setPhase('technical_paused')
  }, [reportClientFailure])

  useEffect(() => {
    let active = true
    activeSessionIdRef.current = sessionId
    disposedRef.current = false
    reconnectAllowedRef.current = true
    sendAudioRef.current = true
    retryRequestedRef.current = false
    captureNeedsRestartRef.current = false
    endingRef.current = false
    pendingEndReasonRef.current = null
    endConfirmedRef.current = false
    manualCompletePendingRef.current = false
    redoInputPendingRef.current = false
    liveTranscriptRef.current = ''
    textTurnPendingRef.current = false
    pendingTextTurnIdRef.current = null
    visitorRevealTextRef.current = ''
    replyCommittedRef.current = false
    replyHasAudioRef.current = false
    playbackEndedSentRef.current = false
    playbackActiveRef.current = false
    technicalPausedRef.current = false
    pendingClientFailuresRef.current = []
    clearVisitorRevealTimer()
    // oxlint-disable-next-line react/set-state-in-effect -- 切换会话时不能短暂显示上一场会谈的未提交内容。
    setLiveTranscript('')
    setTranscript([])
    setVisitorPreview('')
    setVisitorReveal(null)
    setTextTurnStatus('idle')
    setEndedReason(null)
    phaseRef.current = 'listening'
    setPhase('listening')
    setConnection('connecting')
    setTechnicalPause(null)
    setRetrying(false)
    setInputError('')
    setInputNotice('')
    setIsPlaying(false)
    setEnergy(0)
    queueMicrotask(() => {
      if (!active) return
      setManualCompletePending(false)
      setRedoInputPending(false)
      setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
    })
    const createSocket = dependencies.createSocket ?? ((url: string) => new WebSocket(url))
    const createPlayback = dependencies.createPlayback ??
      ((onIdle: () => void) => new PcmAudioPlayback({ onIdle }))
    const createCapture = dependencies.createCapture ??
      ((callbacks: LiveAudioCaptureCallbacks) => new LiveAudioCapture(callbacks))
    let lastEnergyUpdate = Number.NEGATIVE_INFINITY

    const playback = media === 'voice'
      ? createPlayback(() => {
          if (active) finishPlayback()
        })
      : null
    playbackRef.current = playback

    const startTextReveal = (text: string) => {
      clearVisitorRevealTimer()
      const segments = splitOnlineMessages(text)
      visitorRevealTextRef.current = text
      if (segments.length === 0) {
        setVisitorReveal(null)
        return
      }
      let visibleCount = 0
      setVisitorReveal({ turnId: null, visibleSegments: [], isTyping: true })

      const revealNext = () => {
        if (!active || disposedRef.current) return
        visibleCount += 1
        setVisitorReveal((current) => ({
          turnId: current?.turnId ?? null,
          visibleSegments: segments.slice(0, visibleCount),
          isTyping: visibleCount < segments.length,
        }))
        if (visibleCount < segments.length) {
          visitorRevealTimerRef.current = window.setTimeout(revealNext, textMessageIntervalMs)
        } else {
          visitorRevealTimerRef.current = null
        }
      }

      visitorRevealTimerRef.current = window.setTimeout(revealNext, textMessageIntervalMs)
    }

    if (media === 'voice') {
      const capture = createCapture({
        onPcmFrame: (pcm) => {
          if (!active) return
          if (sendAudioRef.current && socketRef.current?.readyState === WebSocket.OPEN) {
            socketRef.current.send(pcm)
          }
        },
        onEnergy: (rms) => {
          if (!active) return
          const now = performance.now()
          if (now - lastEnergyUpdate < 50) return
          lastEnergyUpdate = now
          setEnergy(rms)
        },
        onVadCandidate: (candidate) => {
          if (!active || !sendAudioRef.current) return
          if (candidate.type === 'voice_start') {
            setVoiceActivity({ state: 'speaking', confirmedSilenceMs: 0 })
            sendJson({ type: 'vad.speech_started', at_ms: candidate.atMs })
            return
          }
          setVoiceActivity({
            state: 'paused',
            confirmedSilenceMs: candidate.confirmedSilenceMs,
          })
          sendJson({
            type: 'vad.speech_stopped',
            at_ms: candidate.atMs,
            confirmed_silence_ms: candidate.confirmedSilenceMs,
          })
        },
        onError: (error) => {
          if (active) showCaptureFailure(error)
        },
      })
      captureRef.current = capture
      void capture.start().catch((error) => {
        if (active) showCaptureFailure(error)
      })
    }

    const handleJson = (event: Record<string, unknown>) => {
      if (!active) return
      const type = String(event.type ?? '')
      if (type === 'snapshot') {
        const canRetry = event.can_retry !== false
        const snapshotPhase = (event.phase as RuntimePhase | undefined) ?? 'listening'
        const resumingInterruptedTurn = ['directing', 'acting', 'synthesizing'].includes(snapshotPhase)
        const waitingForRetry = retryRequestedRef.current || resumingInterruptedTurn
        const mustRepeatUnsubmittedVoice = media === 'voice'
          && (Boolean(liveTranscriptRef.current.trim()) || redoInputPendingRef.current)
        const snapshotTranscript = (event.transcript as LiveTurn[] | undefined) ?? []
        setTranscript(snapshotTranscript)
        clearVisitorRevealTimer()
        visitorRevealTextRef.current = ''
        setVisitorReveal(null)
        if (textTurnPendingRef.current && snapshotPhase === 'listening' && !waitingForRetry) {
          const committed = snapshotTranscript.some(
            (turn) => turn.client_turn_id === pendingTextTurnIdRef.current,
          )
          textTurnPendingRef.current = false
          pendingTextTurnIdRef.current = null
          setTextTurnStatus(committed ? 'committed' : 'failed')
        }
        setCanRedoInputCapability(event.can_redo_input !== false)
        setInputNotice('')
        if (mustRepeatUnsubmittedVoice) {
          liveTranscriptRef.current = ''
          setLiveTranscript('')
          redoInputPendingRef.current = false
          setRedoInputPending(false)
          setInputError('刚才这句话没有完整送达，请整句重新说一遍')
        }
        phaseRef.current = snapshotPhase
        setPhase(snapshotPhase)
        if (snapshotPhase === 'listening' && !waitingForRetry) {
          manualCompletePendingRef.current = false
          setManualCompletePending(false)
          redoInputPendingRef.current = false
          setRedoInputPending(false)
        }
        if (snapshotPhase !== 'listening') {
          setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
        }
        technicalPausedRef.current = snapshotPhase === 'technical_paused'
        sendAudioRef.current = snapshotPhase === 'listening'
          && !waitingForRetry
          && !technicalPausedRef.current
          && !endingRef.current
          && !manualCompletePendingRef.current
          && !redoInputPendingRef.current
        if (snapshotPhase === 'technical_paused') {
          reconnectAllowedRef.current = canRetry
          if (!canRetry) stopMedia()
        }
        if (snapshotPhase !== 'technical_paused' && !waitingForRetry) {
          const shouldRestartCapture = captureNeedsRestartRef.current
          retryRequestedRef.current = false
          captureNeedsRestartRef.current = false
          setRetrying(false)
          if (media === 'voice' && shouldRestartCapture) {
            void captureRef.current?.start().catch((error) => {
              if (active) showCaptureFailure(error)
            })
          }
        } else {
          setRetrying(waitingForRetry)
        }
        setTechnicalPause(
          snapshotPhase === 'technical_paused' || waitingForRetry
            ? {
                message: waitingForRetry ? '正在重新接通…' : '来访者的信号不太稳定',
                canRetry,
              }
            : null,
        )
        return
      }
      if (type === 'phase') {
        const nextPhase = event.phase as RuntimePhase
        phaseRef.current = nextPhase
        setPhase(nextPhase)
        setInputNotice('')
        technicalPausedRef.current = nextPhase === 'technical_paused'
        if (nextPhase === 'listening') {
          manualCompletePendingRef.current = false
          setManualCompletePending(false)
          redoInputPendingRef.current = false
          setRedoInputPending(false)
        } else {
          setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
        }
        if (nextPhase !== 'technical_paused') {
          const shouldRestartCapture = captureNeedsRestartRef.current
          retryRequestedRef.current = false
          captureNeedsRestartRef.current = false
          setRetrying(false)
          setTechnicalPause(null)
          if (media === 'voice' && shouldRestartCapture) {
            void captureRef.current?.start().catch((error) => {
              if (active) showCaptureFailure(error)
            })
          }
        }
        sendAudioRef.current = nextPhase === 'listening'
          && !technicalPausedRef.current
          && !endingRef.current
          && !manualCompletePendingRef.current
          && !redoInputPendingRef.current
        return
      }
      if (type === 'asr.partial' || type === 'asr.final') {
        if (redoInputPendingRef.current) return
        const nextTranscript = String(event.transcript ?? event.text ?? '')
        liveTranscriptRef.current = nextTranscript
        setLiveTranscript(nextTranscript)
        setInputNotice('')
        return
      }
      if (type === 'input.reset') {
        redoInputPendingRef.current = false
        setRedoInputPending(false)
        liveTranscriptRef.current = ''
        setLiveTranscript('')
        setInputError('')
        setInputNotice(String(event.message || '已清空，请重新说这一句'))
        setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
        sendAudioRef.current = media === 'voice'
          && phaseRef.current === 'listening'
          && !technicalPausedRef.current
          && !endingRef.current
          && !manualCompletePendingRef.current
        return
      }
      if (type === 'visitor.text') {
        const text = String(event.text ?? '')
        if (media === 'text') {
          setVisitorPreview('')
          startTextReveal(text)
        } else {
          setVisitorPreview(text)
        }
        replyCommittedRef.current = false
        replyHasAudioRef.current = false
        playbackEndedSentRef.current = false
        return
      }
      if (type === 'turn.committed') {
        const workerTurn = event.worker as LiveTurn | undefined
        const clientTurn = event.client as LiveTurn | undefined
        setTranscript((current) => mergeTurns(current, [
          workerTurn,
          clientTurn,
        ]))
        liveTranscriptRef.current = ''
        setLiveTranscript('')
        setVisitorPreview('')
        setInputError('')
        setInputNotice('')
        setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
        redoInputPendingRef.current = false
        setRedoInputPending(false)
        if (
          media === 'text'
          && clientTurn
          && visitorRevealTextRef.current === clientTurn.text
        ) {
          setVisitorReveal((current) => current ? { ...current, turnId: clientTurn.id } : null)
        }
        const committedTurnId = String(event.client_turn_id ?? workerTurn?.client_turn_id ?? '')
        if (
          textTurnPendingRef.current
          && committedTurnId
          && committedTurnId === pendingTextTurnIdRef.current
        ) {
          textTurnPendingRef.current = false
          pendingTextTurnIdRef.current = null
          setTextTurnStatus('committed')
        }
        replyCommittedRef.current = true
        if (media === 'voice' && replyHasAudioRef.current && !playbackActiveRef.current) {
          finishPlayback()
        }
        return
      }
      if (type === 'technical.pause') {
        const canRetry = event.can_retry !== false
        const discardedCurrentInput = redoInputPendingRef.current
        retryRequestedRef.current = false
        reconnectAllowedRef.current = canRetry
        sendAudioRef.current = false
        redoInputPendingRef.current = false
        setRedoInputPending(false)
        if (discardedCurrentInput) {
          liveTranscriptRef.current = ''
          setLiveTranscript('')
        }
        setInputNotice('')
        technicalPausedRef.current = true
        setRetrying(false)
        setTechnicalPause({
          message: String(event.message || '来访者的信号不太稳定'),
          canRetry,
        })
        phaseRef.current = 'technical_paused'
        setPhase('technical_paused')
        if (!canRetry) stopMedia()
        return
      }
      if (type === 'input.error') {
        manualCompletePendingRef.current = false
        redoInputPendingRef.current = false
        sendAudioRef.current = media === 'voice'
          && phaseRef.current === 'listening'
          && !technicalPausedRef.current
          && !endingRef.current
        setManualCompletePending(false)
        setRedoInputPending(false)
        setInputNotice('')
        setInputError(String(event.message || '这次操作没有完成，请重试。'))
        if (textTurnPendingRef.current) {
          textTurnPendingRef.current = false
          pendingTextTurnIdRef.current = null
          setTextTurnStatus('failed')
        }
        return
      }
      if (type === 'session.error') {
        retryRequestedRef.current = false
        setRetrying(false)
        reconnectAllowedRef.current = false
        stopMedia()
        setConnection('closed')
        setInputNotice('')
        setInputError(String(event.message || '本次会谈已经无法继续连接。'))
        if (textTurnPendingRef.current) {
          textTurnPendingRef.current = false
          pendingTextTurnIdRef.current = null
          setTextTurnStatus('failed')
        }
        return
      }
      if (type === 'session.ended') {
        retryRequestedRef.current = false
        setRetrying(false)
        completeEnded(String(event.reason ?? 'user_ended'))
      }
    }

    const connect = () => {
      if (!active || disposedRef.current || !reconnectAllowedRef.current) return
      const socket = createSocket(liveSessionSocketUrl(sessionId))
      socket.binaryType = 'arraybuffer'
      socketRef.current = socket
      socket.onopen = () => {
        if (!active) return
        setConnection('connected')
        setInputError('')
        socket.send(JSON.stringify({ type: 'session.start' }))
        const pendingFailures = pendingClientFailuresRef.current.splice(0)
        for (const failure of pendingFailures) socket.send(JSON.stringify(failure))
        if (retryRequestedRef.current) {
          socket.send(JSON.stringify({ type: 'technical.retry' }))
        }
      }
      socket.onmessage = (message) => {
        if (!active || endConfirmedRef.current) return
        if (typeof message.data === 'string') {
          try {
            handleJson(JSON.parse(message.data) as Record<string, unknown>)
          } catch {
            setInputError('会谈连接收到了一条无法读取的消息。')
          }
          return
        }
        if (message.data instanceof ArrayBuffer) {
          if (media !== 'voice' || !playback) return
          replyHasAudioRef.current = true
          playbackActiveRef.current = true
          setIsPlaying(true)
          void playback.queue(message.data).catch(() => {
            if (active) showPlaybackFailure()
          })
        }
      }
      socket.onclose = (event) => {
        if (!active) return
        detachSocket(socket)
        if (socketRef.current === socket) socketRef.current = null
        setInputNotice('')
        if (event.code === 4409) {
          reconnectAllowedRef.current = false
          stopMedia()
          setConnection('closed')
          clearEndAckTimer()
          void settleEndViaRest(pendingEndReasonRef.current ?? 'user_ended')
          return
        }
        if (event.code === 4404) {
          reconnectAllowedRef.current = false
          stopMedia()
        }
        if (endingRef.current && !endConfirmedRef.current) {
          reconnectAllowedRef.current = false
          setConnection('closed')
          clearEndAckTimer()
          void settleEndViaRest(pendingEndReasonRef.current ?? 'user_ended')
          return
        }
        if (!reconnectAllowedRef.current) {
          stopMedia()
          setConnection('closed')
          return
        }
        if (redoInputPendingRef.current) {
          redoInputPendingRef.current = false
          setRedoInputPending(false)
          setLiveTranscript('')
        }
        setConnection('reconnecting')
        sendAudioRef.current = false
        technicalPausedRef.current = true
        setRetrying(retryRequestedRef.current)
        setTechnicalPause({
          message: retryRequestedRef.current ? '正在重新接通…' : '来访者的信号不太稳定',
          canRetry: true,
        })
        phaseRef.current = 'technical_paused'
        setPhase('technical_paused')
        reconnectTimerRef.current = window.setTimeout(connect, reconnectDelayMs)
      }
      socket.onerror = () => {
        if (active) socket.close()
      }
    }

    connect()
    return () => {
      active = false
      disposedRef.current = true
      reconnectAllowedRef.current = false
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      clearEndAckTimer()
      if (pendingEndReasonRef.current && !endConfirmedRef.current) {
        void settleEndViaRest(pendingEndReasonRef.current)
      }
      const socket = socketRef.current
      if (socket) {
        detachSocket(socket)
        socket.close(1000, 'component_disposed')
        if (socketRef.current === socket) socketRef.current = null
      }
      clearVisitorRevealTimer()
      playback?.stop()
      if (playback) void playback.close()
      void captureRef.current?.close()
      captureRef.current = null
      playbackRef.current = null
    }
  }, [clearEndAckTimer, clearVisitorRevealTimer, completeEnded, dependencies, finishPlayback, media, reconnectDelayMs, sendJson, sessionId, settleEndViaRest, showCaptureFailure, showPlaybackFailure, stopMedia, textMessageIntervalMs])

  const retry = useCallback(() => {
    if (retryRequestedRef.current) return
    reconnectAllowedRef.current = true
    retryRequestedRef.current = true
    sendAudioRef.current = false
    technicalPausedRef.current = true
    setRetrying(true)
    setTechnicalPause({ message: '正在重新接通…', canRetry: true })
    setInputError('')
    setInputNotice('')
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      sendJson({ type: 'technical.retry' })
    }
  }, [sendJson])

  const manualComplete = useCallback(() => {
    if (
      media !== 'voice'
      || connection !== 'connected'
      || phase !== 'listening'
      || technicalPause
      || isPlaying
      || manualCompletePendingRef.current
      || redoInputPendingRef.current
    ) return false
    const sent = sendJson({ type: 'turn.manual_complete', at_ms: performance.now() })
    if (!sent) return false
    setInputError('')
    setInputNotice('')
    manualCompletePendingRef.current = true
    sendAudioRef.current = false
    setManualCompletePending(true)
    setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
    return true
  }, [connection, isPlaying, media, phase, sendJson, technicalPause])

  const redoInput = useCallback(() => {
    if (
      media !== 'voice'
      || connection !== 'connected'
      || phase !== 'listening'
      || technicalPause
      || isPlaying
      || !canRedoInputCapability
      || !liveTranscript.trim()
      || manualCompletePendingRef.current
      || redoInputPendingRef.current
    ) return false
    const sent = sendJson({ type: 'turn.redo_input' })
    if (!sent) return false
    setInputError('')
    setInputNotice('')
    redoInputPendingRef.current = true
    sendAudioRef.current = false
    setRedoInputPending(true)
    setVoiceActivity({ state: 'quiet', confirmedSilenceMs: 0 })
    return true
  }, [canRedoInputCapability, connection, isPlaying, liveTranscript, media, phase, sendJson, technicalPause])

  const sendText = useCallback((text: string) => {
    const value = text.trim()
    if (
      media !== 'text'
      || !value
      || connection !== 'connected'
      || phase !== 'listening'
      || technicalPause
      || textTurnPendingRef.current
    ) return false
    const clientTurnId = makeId()
    setInputError('')
    setInputNotice('')
    const sent = sendJson({ type: 'text.turn', text: value, client_turn_id: clientTurnId })
    if (!sent) return false
    textTurnPendingRef.current = true
    pendingTextTurnIdRef.current = clientTurnId
    setTextTurnStatus('pending')
    return true
  }, [connection, media, phase, sendJson, technicalPause])

  const endSession = useCallback(() => {
    if (endingRef.current) return
    endingRef.current = true
    reconnectAllowedRef.current = false
    stopMedia()
    const reason: EndRequestReason = technicalPausedRef.current
      ? 'technical_interruption'
      : 'user_ended'
    pendingEndReasonRef.current = reason
    if (sendJson({ type: 'session.end' })) {
      armEndAckTimer(reason)
      return
    }
    void settleEndViaRest(reason)
  }, [armEndAckTimer, sendJson, settleEndViaRest, stopMedia])

  return {
    connection,
    phase,
    transcript,
    liveTranscript,
    visitorPreview,
    visitorReveal,
    textTurnStatus,
    technicalPause,
    retrying,
    inputError,
    inputNotice,
    isPlaying,
    endedReason,
    energy,
    voiceActivity,
    canManualComplete: media === 'voice'
      && connection === 'connected'
      && phase === 'listening'
      && !technicalPause
      && !isPlaying
      && !manualCompletePending
      && !redoInputPending,
    manualCompletePending,
    canRedoInput: media === 'voice'
      && connection === 'connected'
      && canRedoInputCapability
      && phase === 'listening'
      && !technicalPause
      && !isPlaying
      && !manualCompletePending
      && !redoInputPending
      && Boolean(liveTranscript.trim()),
    redoInputPending,
    retry,
    redoInput,
    manualComplete,
    sendText,
    endSession,
  }
}
