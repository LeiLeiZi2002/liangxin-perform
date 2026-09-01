import { useQuery, useQueryClient } from '@tanstack/react-query'
import { CircleStop, Mic, PhoneOff, RotateCcw, Send } from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { getSession } from '../api/client'
import type { Session } from '../api/contracts'
import { endStateCopy, livePhaseLabels } from '../api/labels'
import {
  splitOnlineMessages,
  useLiveSession,
  type LiveTurn,
  type VisitorReveal,
} from '../features/live-session/use-live-session'

const sceneLabels = {
  hotline: '心理热线',
  institution: '机构面谈',
  online: '在线咨询',
} as const

function formatDuration(seconds: number) {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

function useActiveClock(frozen: boolean) {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    if (frozen) return
    const timer = window.setInterval(() => setSeconds((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [frozen])
  return seconds
}

function useOnlineScrollFollow(content: unknown) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const nearBottomRef = useRef(true)

  const trackPosition = () => {
    const element = scrollRef.current
    if (!element) return
    nearBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight <= 56
  }

  useLayoutEffect(() => {
    const element = scrollRef.current
    if (element && nearBottomRef.current) element.scrollTop = element.scrollHeight
  }, [content])

  return { scrollRef, trackPosition }
}

function endingCopyForMedia(reason: string | null, media: Session['media']) {
  if (media === 'voice') return endStateCopy(reason)
  if (reason === 'natural_closure') {
    return {
      title: '本次咨询已自然结束',
      detail: '来访者已经结束在线咨询，本次会谈原文已完整保留。',
    }
  }
  if (reason === 'technical_interruption') {
    return {
      title: '咨询因连接中断结束',
      detail: '已经确认的文字内容已保留，可以继续完成工作记录。',
    }
  }
  return {
    title: '你已结束本次咨询',
    detail: '本次会谈原文已保存，可以继续填写工作记录。',
  }
}

function TranscriptTurn({
  turn,
  online,
  reveal,
}: {
  turn: LiveTurn
  online: boolean
  reveal?: VisitorReveal | null
}) {
  if (!online) {
    return (
      <li
        className={`live-turn live-turn--${turn.speaker}`}
        data-client-turn-id={turn.client_turn_id}
        data-turn-id={turn.id}
      >
        <span>{turn.speaker === 'worker' ? '你' : '来访者'}</span>
        <p>{turn.text}</p>
      </li>
    )
  }

  const revealMatches = turn.speaker === 'client' && reveal?.turnId === turn.id
  const segments = turn.speaker === 'client'
    ? revealMatches
      ? reveal.visibleSegments
      : splitOnlineMessages(turn.text)
    : [turn.text.trim()]

  return (
    <li
      className={`live-turn online-message online-message--${turn.speaker}`}
      data-client-turn-id={turn.client_turn_id}
      data-turn-id={turn.id}
    >
      <span>{turn.speaker === 'worker' ? '你' : '来访者'}</span>
      <div className="online-message-stack">
        {segments.map((segment, index) => (
          <p className="online-message-bubble" key={`${turn.id}-${index}`}>{segment}</p>
        ))}
        {revealMatches && reveal.isTyping ? <TypingIndicator /> : null}
      </div>
    </li>
  )
}

function TypingIndicator() {
  return (
    <span className="online-typing" role="status" aria-live="polite">
      <i aria-hidden="true" /><i aria-hidden="true" /><i aria-hidden="true" />
      对方正在输入…
    </span>
  )
}

function TranscriptList({
  turns,
  online,
  liveTranscript = '',
  visitorPreview = '',
  visitorReveal = null,
}: {
  turns: LiveTurn[]
  online: boolean
  liveTranscript?: string
  visitorPreview?: string
  visitorReveal?: VisitorReveal | null
}) {
  const revealIsCommitted = Boolean(
    visitorReveal?.turnId && turns.some((turn) => turn.id === visitorReveal.turnId),
  )

  return (
    <ol className={`live-transcript${online ? ' online-transcript' : ''}`}>
      {turns.map((turn) => (
        <TranscriptTurn key={turn.id} turn={turn} online={online} reveal={visitorReveal} />
      ))}
      {!online && liveTranscript ? (
        <li className="live-turn live-turn--worker live-turn--provisional" aria-live="polite">
          <span>识别中，文字可能继续修正</span><p>{liveTranscript}</p>
        </li>
      ) : null}
      {!online && visitorPreview ? (
        <li className="live-turn live-turn--client live-turn--provisional" aria-live="polite">
          <span>来访者正在说</span><p>{visitorPreview}</p>
        </li>
      ) : null}
      {online && visitorReveal && !revealIsCommitted ? (
        <li className="live-turn online-message online-message--client online-message--provisional">
          <span>来访者</span>
          <div className="online-message-stack">
            {visitorReveal.visibleSegments.map((segment, index) => (
              <p className="online-message-bubble" key={`preview-${index}`}>{segment}</p>
            ))}
            {visitorReveal.isTyping ? <TypingIndicator /> : null}
          </div>
        </li>
      ) : null}
    </ol>
  )
}

function PersistedEndedSession({ session, turns }: { session: Session; turns: LiveTurn[] }) {
  const [transcriptExpanded, setTranscriptExpanded] = useState(false)
  const online = session.media === 'text'
  const endedLabel = online ? '咨询结束' : '通话结束'
  const endingCopy = endingCopyForMedia(session.end_reason, session.media)

  return (
    <main className={`live-session-page live-session-page--${session.media} page-enter`}>
      <header className="call-bar">
        <div className="call-bar__identity">
          <span className="call-bar__scene">{sceneLabels[session.scene!]}</span>
          <strong>来访者 · 匿名</strong>
        </div>
        <div className="call-bar__status" aria-live="polite">
          <span className="connection-dot connection-dot--closed" />
          {online ? '咨询已经结束' : '通话已经结束'}
        </div>
      </header>

      <section className="signal-pause" role="status" aria-label={endedLabel}>
        <div><span>{endedLabel}</span><h2>{endingCopy.title}</h2></div>
        <p>{endingCopy.detail}</p>
        <div className="signal-pause__actions">
          {session.mode === 'assessment' ? (
            <Link className="button button--coral" to={`/sessions/${session.id}/work-record`}>
              填写工作记录
            </Link>
          ) : <Link className="button button--ink" to="/">返回首页</Link>}
        </div>
      </section>

      <section className="transcript-sheet" aria-labelledby="persisted-transcript-heading">
        <header>
          <div><span>本次会谈</span><h2 id="persisted-transcript-heading">会谈原文</h2></div>
          <button
            className="button"
            type="button"
            aria-expanded={transcriptExpanded}
            aria-controls="persisted-transcript-content"
            onClick={() => setTranscriptExpanded((expanded) => !expanded)}
          >
            {transcriptExpanded ? '收起会谈原文' : '展开会谈原文'}
          </button>
        </header>
        {transcriptExpanded ? (
          <div id="persisted-transcript-content">
            <TranscriptList turns={turns} online={online} />
          </div>
        ) : <div className="transcript-empty">已记录 {turns.length} 条确认内容</div>}
      </section>
    </main>
  )
}

function SessionWorkbench({ session, initialTurns }: { session: Session; initialTurns: LiveTurn[] }) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState('')
  const [draftSubmitted, setDraftSubmitted] = useState(false)
  const [confirmEnd, setConfirmEnd] = useState(false)
  const [transcriptExpanded, setTranscriptExpanded] = useState(true)
  const live = useLiveSession(session.id, session.media)
  const frozen = Boolean(live.technicalPause) || live.phase === 'ended'
  const elapsed = useActiveClock(frozen)
  const voice = session.media === 'voice'
  const turns = live.transcript.length > 0 ? live.transcript : initialTurns
  const onlineScrollContent = `${turns.at(-1)?.id ?? ''}:${turns.length}:${live.visitorReveal?.visibleSegments.join('\n') ?? ''}:${live.visitorReveal?.isTyping ?? false}`
  const { scrollRef: onlineScrollRef, trackPosition: trackOnlineScroll } = useOnlineScrollFollow(onlineScrollContent)
  const currentPhase = livePhaseLabels[live.phase]
  const connected = live.connection === 'connected'
  const endLabel = session.mode === 'assessment' ? '立即挂断并结束' : '立即结束本次体验'
  const ended = live.phase === 'ended' || Boolean(live.endedReason)
  const endingCopy = endStateCopy(live.endedReason)
  const canManualComplete = connected
    && live.phase === 'listening'
    && !live.technicalPause
    && !live.isPlaying
    && live.canManualComplete

  useEffect(() => {
    if (!live.endedReason) return
    const endedAt = new Date().toISOString()
    queryClient.setQueryData<{ session: Session; transcript: LiveTurn[] }>(
      ['session', session.id],
      (current) => current ? {
        ...current,
        session: {
          ...current.session,
          status: 'ended',
          end_reason: live.endedReason as Session['end_reason'],
          ended_at: endedAt,
          updated_at: endedAt,
        },
      } : current,
    )
  }, [live.endedReason, queryClient, session.id])

  const orbScale = useMemo(() => 1 + Math.min(live.energy, 0.12) * 1.7, [live.energy])
  const draftValue = live.textTurnStatus === 'committed' && draftSubmitted ? '' : draft

  function submitText() {
    if (
      !connected
      || live.phase !== 'listening'
      || live.technicalPause
      || live.textTurnStatus === 'pending'
      || !draftValue.trim()
    ) return
    if (live.sendText(draftValue)) setDraftSubmitted(true)
  }

  const connectionLabel = live.connection === 'connecting'
    ? '正在接通…'
    : live.connection === 'reconnecting'
      ? '正在重新接通…'
      : live.connection === 'closed' && !ended
        ? '连接已断开'
        : currentPhase

  const conversationHint = ended
    ? '通话已经结束，麦克风与声音播放均已关闭。'
    : !voice
      ? '可以按真实在线咨询的方式组织文字。'
      : live.isPlaying
        ? '来访者正在说话，请听完后再继续回应。'
        : live.voiceActivity.state === 'paused'
          ? '检测到停顿，确认说完后请点击“我说完了”。'
          : live.voiceActivity.state === 'speaking'
            ? '正在听你说话，本轮结束后请点击“我说完了”。'
            : '按平常的节奏说就好，本轮只会在你点击“我说完了”后提交。'

  if (!voice) {
    const onlineConnectionLabel = live.connection === 'connecting'
      ? '正在进入咨询…'
      : live.connection === 'reconnecting'
        ? '正在恢复会话…'
        : live.connection === 'closed' && !ended
          ? '连接已断开'
          : live.phase === 'listening'
            ? '在线'
            : live.phase === 'ended'
              ? '本次咨询已结束'
              : live.phase === 'technical_paused'
                ? '会话暂时中断'
                : '来访者正在输入…'
    const onlineEndingCopy = endingCopyForMedia(live.endedReason, session.media)
    const textPending = live.textTurnStatus === 'pending'
    const inputDisabled = !connected
      || live.phase !== 'listening'
      || Boolean(live.technicalPause)
      || textPending

    return (
      <main className="live-session-page live-session-page--text page-enter">
        <section className="online-chat-workbench" aria-label="在线咨询工作台">
          <header className="online-chat-header">
            <div className="online-chat-header__identity">
              <span>在线咨询</span>
              <strong>来访者 · 匿名</strong>
            </div>
            <div className="online-chat-header__status" aria-live="polite">
              <span className={`connection-dot connection-dot--${live.connection}`} />
              {onlineConnectionLabel}
            </div>
            <div className="call-clock">
              <span>{ended ? '咨询时长' : frozen ? '计时已暂停' : '咨询时间'}</span>
              <time>{formatDuration(elapsed)}</time>
            </div>
          </header>

          <section className="online-chat-messages" role="region" aria-label="在线咨询消息">
            <header>
              <div><span>本次会谈</span><h2>咨询记录</h2></div>
              <button
                className="button"
                type="button"
                aria-expanded={transcriptExpanded}
                aria-controls="online-transcript-content"
                onClick={() => setTranscriptExpanded((expanded) => !expanded)}
              >
                {transcriptExpanded ? '收起会谈原文' : '展开会谈原文'}
              </button>
            </header>
            {!transcriptExpanded ? (
              <div className="transcript-empty">
                {turns.length > 0 ? `已记录 ${turns.length} 条确认内容` : '进入咨询后，消息会显示在这里。'}
              </div>
            ) : (
              <div
                id="online-transcript-content"
                className="online-chat-scroll"
                ref={onlineScrollRef}
                onScroll={trackOnlineScroll}
              >
                {turns.length === 0 && !live.visitorReveal ? (
                  <div className="transcript-empty">进入咨询后，消息会显示在这里。</div>
                ) : (
                  <TranscriptList
                    turns={turns}
                    online
                    visitorReveal={live.visitorReveal}
                  />
                )}
              </div>
            )}
          </section>

          {ended ? (
            <section className="signal-pause" role="status" aria-label="咨询结束">
              <div><span>咨询结束</span><h2>{onlineEndingCopy.title}</h2></div>
              <p>{onlineEndingCopy.detail}</p>
              <div className="signal-pause__actions">
                {session.mode === 'assessment' ? (
                  <Link className="button button--coral" to={`/sessions/${session.id}/work-record`}>
                    填写工作记录
                  </Link>
                ) : <Link className="button button--ink" to="/">返回首页</Link>}
              </div>
            </section>
          ) : null}

          {live.technicalPause && !ended ? (
            <section className="signal-pause" role="alert">
              <div><span>连接提示</span><h2>{live.technicalPause.message}</h2></div>
              <p>已经确认的文字内容会留在这里，暂停期间不会计入咨询时间。</p>
              <div className="signal-pause__actions">
                {live.technicalPause.canRetry ? (
                  <button
                    className="button button--ink"
                    type="button"
                    disabled={live.retrying}
                    onClick={live.retry}
                  >
                    <RotateCcw aria-hidden="true" size={16} />
                    {live.retrying ? '正在恢复会话…' : '重新连接'}
                  </button>
                ) : <Link className="button button--ink" to="/configure">前往设置</Link>}
                <button className="button signal-pause__end" type="button" onClick={() => setConfirmEnd(true)}>
                  结束本次咨询
                </button>
              </div>
            </section>
          ) : null}

          {live.inputError ? <p className="session-inline-error" role="alert">{live.inputError}</p> : null}
          {live.inputNotice ? (
            <p className="session-inline-notice" role="status" aria-live="polite">{live.inputNotice}</p>
          ) : null}

          {!ended ? (
            <footer className="online-chat-controls">
              <div className="text-composer">
                <label htmlFor="live-text-input">输入本轮内容</label>
                <textarea
                  id="live-text-input"
                  aria-label="输入本轮内容"
                  value={draftValue}
                  disabled={inputDisabled}
                  placeholder="在这里输入你要对来访者说的话…"
                  onChange={(event) => {
                    setDraft(event.target.value)
                    setDraftSubmitted(false)
                  }}
                  onKeyDown={(event) => {
                    if (event.ctrlKey && event.key === 'Enter') {
                      event.preventDefault()
                      submitText()
                    }
                  }}
                />
                <button
                  className="button button--coral"
                  type="button"
                  disabled={!draftValue.trim() || inputDisabled}
                  onClick={submitText}
                >
                  <Send aria-hidden="true" size={16} />{textPending ? '发送中…' : '发送'}
                </button>
                <small>{textPending ? '正在等待来访者收到这条消息' : 'Ctrl + Enter 发送'}</small>
              </div>
              <button className="end-session-button" type="button" onClick={() => setConfirmEnd(true)}>
                结束本次咨询
              </button>
            </footer>
          ) : null}
        </section>

        {confirmEnd && !ended ? (
          <div className="dialog-backdrop" role="presentation">
            <section className="end-dialog" role="dialog" aria-modal="true" aria-label="确认结束在线咨询">
              <span>结束确认</span>
              <h2>确定现在结束咨询吗？</h2>
              <p>{session.mode === 'assessment'
                ? '确认后会立即结束，不会再等待来访者回应；当前原文会完整保留。'
                : '确认后会立即结束，已经发生的对话会保留在本次体验中。'}</p>
              <div>
                <button className="button button--coral" type="button" onClick={() => { setConfirmEnd(false); live.endSession() }}>
                  确认结束咨询
                </button>
                <button className="button" type="button" onClick={() => setConfirmEnd(false)}>继续会谈</button>
              </div>
            </section>
          </div>
        ) : null}
      </main>
    )
  }

  return (
    <main className={`live-session-page live-session-page--${session.media} page-enter`}>
      <div className="session-workbench-grid">
      <section className="session-call-column" aria-label="通话与控制">
      <header className="call-bar">
        <div className="call-bar__identity">
          <span className="call-bar__scene">{sceneLabels[session.scene!]}</span>
          <strong>来访者 · 匿名</strong>
        </div>
        <div className="call-bar__status" aria-live="polite">
          <span className={`connection-dot connection-dot--${live.connection}`} />
          {connectionLabel}
        </div>
        <div className="call-clock">
          <span>{ended ? '通话时长' : frozen ? '计时已暂停' : '会谈时间'}</span>
          <time>{formatDuration(elapsed)}</time>
        </div>
      </header>

      <section className="conversation-stage" aria-label="当前会谈状态">
        <div className={`voice-presence voice-presence--${live.phase}`}>
          <span className="voice-presence__halo" style={{ transform: `scale(${orbScale})` }} />
          <span className="voice-presence__core">
            <Mic aria-hidden="true" size={30} strokeWidth={1.35} />
          </span>
        </div>
        <p className="conversation-stage__status">{connectionLabel}</p>
        <p className="conversation-stage__hint">{conversationHint}</p>
        {voice && !ended && connected && !live.technicalPause ? (
          <span className="microphone-state"><Mic aria-hidden="true" size={14} />麦克风已连接</span>
        ) : null}
      </section>

      {ended ? (
        <section className="signal-pause" role="status" aria-label="通话结束">
          <div><span>通话结束</span><h2>{endingCopy.title}</h2></div>
          <p>{endingCopy.detail}</p>
          <div className="signal-pause__actions">
            {session.mode === 'assessment' ? (
              <Link className="button button--coral" to={`/sessions/${session.id}/work-record`}>
                填写工作记录
              </Link>
            ) : (
              <Link className="button button--ink" to="/">返回首页</Link>
            )}
          </div>
        </section>
      ) : null}

      {live.technicalPause && !ended ? (
        <section className="signal-pause" role="alert">
          <div><span>连接提示</span><h2>{live.technicalPause.message}</h2></div>
          <p>已经确认的会谈原文会留在这里，暂停期间不会计入会谈时间。</p>
          <div className="signal-pause__actions">
            {live.technicalPause.canRetry ? (
              <button
                className="button button--ink"
                type="button"
                disabled={live.retrying}
                onClick={live.retry}
              >
                <RotateCcw aria-hidden="true" size={16} />
                {live.retrying ? '正在重新接通…' : '重新连接'}
              </button>
            ) : <Link className="button button--ink" to="/configure">前往设置</Link>}
            <button className="button signal-pause__end" type="button" onClick={() => setConfirmEnd(true)}>
              结束技术中断
            </button>
          </div>
        </section>
      ) : null}

      {live.inputError ? <p className="session-inline-error" role="alert">{live.inputError}</p> : null}
      {live.inputNotice ? (
        <p className="session-inline-notice" role="status" aria-live="polite">
          {live.inputNotice}
        </p>
      ) : null}

      {!ended ? <footer className="session-controls">
        <div className="voice-controls">
            <div className="voice-control-actions">
              <button
                className="button button--ink manual-complete"
                type="button"
                disabled={!canManualComplete}
                onClick={live.manualComplete}
              >
                <CircleStop aria-hidden="true" size={17} />
                {live.manualCompletePending ? '正在提交…' : '我说完了'}
              </button>
              <button
                className="button voice-redo"
                type="button"
                disabled={!live.canRedoInput}
                onClick={live.redoInput}
              >
                <RotateCcw aria-hidden="true" size={16} />
                {live.redoInputPending ? '正在清空…' : '重新说这句'}
              </button>
            </div>
            <p>文字有误时，先点“重新说这句”，再重新说；确认后点“我说完了”。</p>
        </div>
        <button className="end-session-button" type="button" onClick={() => setConfirmEnd(true)}>
          <PhoneOff aria-hidden="true" size={16} />{endLabel}
        </button>
      </footer> : null}
      </section>

      <section className="transcript-sheet" aria-label="会谈原文" aria-labelledby="transcript-heading">
        <header>
          <div><span>本次会谈</span><h2 id="transcript-heading">会谈原文</h2></div>
          <button
            className="button"
            type="button"
            aria-expanded={transcriptExpanded}
            aria-controls="live-transcript-content"
            onClick={() => setTranscriptExpanded((expanded) => !expanded)}
          >
            {transcriptExpanded ? '收起会谈原文' : '展开会谈原文'}
          </button>
        </header>
        {!transcriptExpanded ? (
          <div className="transcript-empty">
            {turns.length > 0 ? `已记录 ${turns.length} 条确认内容` : '接通后，对话会记录在这里。'}
          </div>
        ) : (
          <div id="live-transcript-content">
            {turns.length === 0 && !live.liveTranscript && !live.visitorPreview ? (
              <div className="transcript-empty">接通后，对话会从这里开始。</div>
            ) : (
              <TranscriptList
                turns={turns}
                online={false}
                liveTranscript={live.liveTranscript}
                visitorPreview={live.visitorPreview}
              />
            )}
          </div>
        )}
      </section>
      </div>

      {confirmEnd && !ended ? (
        <div className="dialog-backdrop" role="presentation">
          <section className="end-dialog" role="dialog" aria-modal="true" aria-label="确认挂断通话">
            <span>结束确认</span>
            <h2>确定现在挂断吗？</h2>
            <p>{session.mode === 'assessment' ? '确认后会立即结束，不会再等待来访者回应；当前原文会完整保留。' : '确认后会立即结束，已经发生的对话会保留在本次体验中。'}</p>
            <div>
              <button className="button button--coral" type="button" onClick={() => { setConfirmEnd(false); live.endSession() }}>确认立即挂断</button>
              <button className="button" type="button" onClick={() => setConfirmEnd(false)}>继续会谈</button>
            </div>
          </section>
        </div>
      ) : null}
    </main>
  )
}

export function SessionPage() {
  const { sessionId = 'new' } = useParams()
  const detail = useQuery({
    queryKey: ['session', sessionId],
    enabled: sessionId !== 'new',
    queryFn: () => getSession(sessionId),
  })

  if (sessionId === 'new') {
    return <main className="error-sheet"><h1>还没有建立会谈</h1><p>请从测评或体验入口重新开始。</p></main>
  }
  if (detail.isLoading) {
    return <main className="loading-sheet"><span className="loading-sheet__mark" /><p>正在接通会谈…</p><small>会谈原文会在连接后恢复</small></main>
  }
  if (detail.isError || !detail.data) {
    return <main className="error-sheet"><h1>暂时没有接通</h1><p>请确认本地服务仍在运行。</p><button className="button button--ink" onClick={() => void detail.refetch()}>重新加载</button></main>
  }

  const initialTurns: LiveTurn[] = detail.data.transcript.map((turn) => ({
    id: turn.id,
    sequence: turn.sequence,
    speaker: turn.speaker,
    text: turn.text,
    client_turn_id: turn.client_turn_id,
  }))
  if (detail.data.session.status === 'ended') {
    return <PersistedEndedSession session={detail.data.session} turns={initialTurns} />
  }
  return <SessionWorkbench session={detail.data.session} initialTurns={initialTurns} />
}
