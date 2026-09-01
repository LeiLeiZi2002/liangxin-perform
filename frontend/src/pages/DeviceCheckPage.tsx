import { Check, CircleAlert, KeyRound, Laptop, Mic, PhoneIncoming } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { getHealth, getProviderConfig } from '../api/client'

type CheckId = 'service' | 'credentials' | 'microphone'

type ItemState = {
  status: 'idle' | 'checking' | 'passed' | 'failed' | 'skipped'
  message: string
}

const idle = (): ItemState => ({ status: 'idle', message: '等待检查' })
const skipped = (): ItemState => ({ status: 'skipped', message: '本场域无需检查' })

const ANSWER_FEEDBACK_MS = 360

function useIncomingCallTone(active: boolean) {
  useEffect(() => {
    if (!active) return
    const AudioContextConstructor = window.AudioContext
      ?? (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AudioContextConstructor) return

    let context: AudioContext
    try {
      context = new AudioContextConstructor()
    } catch {
      return
    }

    const output = context.createGain()
    const oscillators = [440, 480].map((frequency) => {
      const oscillator = context.createOscillator()
      oscillator.type = 'sine'
      oscillator.frequency.value = frequency
      oscillator.connect(output)
      oscillator.start()
      return oscillator
    })
    output.gain.value = 0.0001
    output.connect(context.destination)

    const start = context.currentTime
    for (let cycle = 0; cycle < 24; cycle += 1) {
      const at = start + cycle * 2.7
      output.gain.setValueAtTime(0.0001, at)
      output.gain.linearRampToValueAtTime(0.035, at + 0.025)
      output.gain.setValueAtTime(0.035, at + 0.43)
      output.gain.linearRampToValueAtTime(0.0001, at + 0.48)
      output.gain.setValueAtTime(0.0001, at + 0.63)
      output.gain.linearRampToValueAtTime(0.035, at + 0.655)
      output.gain.setValueAtTime(0.035, at + 1.06)
      output.gain.linearRampToValueAtTime(0.0001, at + 1.11)
    }
    void context.resume().catch(() => undefined)

    return () => {
      for (const oscillator of oscillators) {
        try {
          oscillator.stop()
        } catch {
          // 节点已经停止时无需再处理。
        }
        oscillator.disconnect()
      }
      output.disconnect()
      void context.close().catch(() => undefined)
    }
  }, [active])
}

function statusText(item: ItemState) {
  if (item.status === 'passed') return '已就绪'
  if (item.status === 'checking') return '检查中…'
  return item.message
}

export function DeviceCheckPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const scene = params.get('scene') ?? 'hotline'
  const voice = scene !== 'online'
  const [running, setRunning] = useState(false)
  const [hasRun, setHasRun] = useState(false)
  const [answering, setAnswering] = useState(false)
  const answerTimerRef = useRef<number | null>(null)
  const [items, setItems] = useState<Record<CheckId, ItemState>>({
    service: idle(),
    credentials: idle(),
    microphone: voice ? idle() : skipped(),
  })

  const rows = [
    { id: 'service' as const, title: '本地服务', note: '确认 DEMO 已在本机启动', icon: Laptop },
    { id: 'credentials' as const, title: '模型调用凭证', note: '只确认是否配置，不发起模型调用', icon: KeyRound },
    { id: 'microphone' as const, title: '麦克风', note: '短暂取得权限后立即关闭', icon: Mic },
  ]

  const ready = useMemo(() => {
    const required: CheckId[] = voice
      ? ['service', 'credentials', 'microphone']
      : ['service', 'credentials']
    return required.every((id) => items[id].status === 'passed')
  }, [items, voice])
  const needsKey = items.credentials.status === 'failed'
    && items.credentials.message === '尚未配置模型调用凭证'
  const backendUnavailable = items.service.status === 'failed'

  useIncomingCallTone(ready && voice && !answering)

  useEffect(() => () => {
    if (answerTimerRef.current !== null) window.clearTimeout(answerTimerRef.current)
  }, [])

  async function checkMicrophone(): Promise<ItemState> {
    if (!voice) return skipped()
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      })
      stream.getTracks().forEach((track) => track.stop())
      return { status: 'passed', message: '已就绪' }
    } catch {
      return { status: 'failed', message: '请允许浏览器使用麦克风' }
    }
  }

  async function runCheck() {
    if (running) return
    setRunning(true)
    setHasRun(true)
    setItems({
      service: { status: 'checking', message: '检查中…' },
      credentials: { status: 'checking', message: '检查中…' },
      microphone: voice ? { status: 'checking', message: '检查中…' } : skipped(),
    })

    const [service, credentials, microphone] = await Promise.all([
      getHealth()
        .then((): ItemState => ({ status: 'passed', message: '已就绪' }))
        .catch((): ItemState => ({ status: 'failed', message: '本地服务暂时没有回应' })),
      getProviderConfig()
        .then((config): ItemState => config.configured
          ? { status: 'passed', message: '已就绪' }
          : { status: 'failed', message: '尚未配置模型调用凭证' })
        .catch((): ItemState => ({ status: 'failed', message: '暂时无法读取服务配置' })),
      checkMicrophone(),
    ])
    setItems({ service, credentials, microphone })
    setRunning(false)
  }

  function enterSession() {
    if (!ready || answering) return
    const sessionId = params.get('sessionId') ?? 'new'
    const forwarded = new URLSearchParams(params)
    forwarded.delete('sessionId')
    const query = forwarded.toString()
    const target = `/session/${sessionId}${query ? `?${query}` : ''}`
    if (!voice) {
      navigate(target)
      return
    }
    setAnswering(true)
    answerTimerRef.current = window.setTimeout(() => {
      answerTimerRef.current = null
      navigate(target)
    }, ANSWER_FEEDBACK_MS)
  }

  return (
    <main className="device-check-page page-enter">
      <header className="device-check-heading">
        <div>
          <p className="archive-kicker">开始前 · 通话准备</p>
          <h1>{voice ? '先确认这通热线能顺利接通' : '先确认在线交流能够开始'}</h1>
        </div>
        <p>{voice
          ? '检查只确认本地服务、调用凭证和麦克风权限，不会提前调用模型，也不会产生试呼费用。'
          : '检查只确认本地服务和调用凭证，不会提前调用模型。'}</p>
      </header>

      <section className="device-check-panel" aria-label="检查项目">
        <div className="device-check-panel__intro">
          <span>{voice ? '语音会谈' : '文字会谈'}</span>
          <strong>{scene === 'hotline' ? '心理热线' : scene === 'institution' ? '机构面谈' : '在线咨询'}</strong>
          <button className="button button--ink" type="button" disabled={running} onClick={runCheck}>
            {running ? '正在检查…' : hasRun ? '重新检查' : '开始检查'}
          </button>
        </div>
        <ol className="readiness-list">
          {rows.map(({ id, title, note, icon: Icon }, index) => {
            const item = items[id]
            return (
              <li key={id} className={`readiness-item readiness-item--${item.status}`}>
                <span className="readiness-item__number">{String(index + 1).padStart(2, '0')}</span>
                <Icon aria-hidden="true" size={20} strokeWidth={1.5} />
                <span className="readiness-item__copy"><strong>{title}</strong><small>{note}</small></span>
                <span className="readiness-item__result">
                  {item.status === 'passed' ? <Check aria-hidden="true" size={16} /> : null}
                  {item.status === 'failed' ? <CircleAlert aria-hidden="true" size={16} /> : null}
                  {statusText(item)}
                </span>
              </li>
            )
          })}
        </ol>
      </section>

      {needsKey ? (
        <p className="readiness-notice" role="alert">
          调用凭证还没有配置。<Link to="/configure">前往设置</Link>
        </p>
      ) : null}
      {backendUnavailable ? (
        <p className="readiness-notice" role="alert">
          请确认已运行项目根目录下的“启动DEMO.cmd”，再重新检查。
        </p>
      ) : null}
      {items.microphone.status === 'failed' ? (
        <p className="readiness-notice" role="alert">
          请在浏览器地址栏的权限设置中允许麦克风，然后重新检查。
        </p>
      ) : null}
      <footer className={ready && voice ? 'incoming-call' : 'device-check-actions'} aria-live="polite">
        {ready && voice ? (
          <div className="incoming-call__identity">
            <span className="incoming-call__icon" aria-hidden="true"><PhoneIncoming size={23} strokeWidth={1.5} /></span>
            <span className="incoming-call__copy">
              <small>匿名来电</small>
              <strong>心理热线来电</strong>
              <span role="status">{answering ? '正在接听…' : '正在呼入…'}</span>
            </span>
          </div>
        ) : (
          <p>{ready
            ? '准备完成，可以进入会谈。'
            : `${voice ? '三项' : '两项'}准备就绪后，才能进入会谈。`}</p>
        )}
        <button className="button button--coral" type="button" disabled={!ready || running || answering} onClick={enterSession}>
          {voice ? answering ? '正在接听…' : '接听来电' : '进入会谈'}
        </button>
      </footer>
    </main>
  )
}
