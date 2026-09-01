import { useMutation, useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError, createSession, drawCase, getDemoConfig } from '../api/client'
import type { CaseType, Scene } from '../api/contracts'

const sceneOptions = [
  { value: 'institution', name: '机构面谈', media: '实时语音', unavailable: true },
  { value: 'hotline', name: '心理热线', media: '实时语音', unavailable: false },
  { value: 'online', name: '在线咨询', media: '实时文字', unavailable: false },
] as const satisfies readonly {
  value: Scene
  name: string
  media: string
  unavailable: boolean
}[]

const caseOptions = [
  { value: 'main', name: '主个案', note: '较完整的情境，需要更充分地展开沟通' },
  { value: 'short', name: '短个案', note: '较紧凑的情境，沟通内容相对集中' },
] as const satisfies readonly { value: CaseType; name: string; note: string }[]

export function StartPage({ mode }: { mode: 'assessment' | 'experience' }) {
  const [scene, setScene] = useState<Scene>('hotline')
  const [caseType, setCaseType] = useState<CaseType>('main')
  const [configRefreshPending, setConfigRefreshPending] = useState(false)
  const assessment = mode === 'assessment'
  const assessmentConfig = useQuery({
    queryKey: ['demo-config'],
    queryFn: getDemoConfig,
    enabled: assessment,
    retry: false,
  })
  const navigate = useNavigate()
  const create = useMutation({
    mutationFn: async () => {
      let selectedScene = scene
      let selectedCaseType = caseType
      if (assessment) {
        const config = assessmentConfig.data
        if (!config || assessmentConfig.isFetching) {
          throw new Error('正式测评配置尚未读取完成')
        }
        selectedScene = config.scene
        selectedCaseType = config.case_type
      }
      const selection = {
        mode,
        scene: selectedScene,
        caseType: selectedCaseType,
      }
      const chosen = await drawCase(selection.scene, selection.caseType)
      const session = await createSession({
        mode: selection.mode,
        scene: selection.scene,
        case_type: selection.caseType,
        case_id: chosen.case_id,
      })
      return { session, selection }
    },
    onSuccess: ({ session, selection }) => {
      const query = new URLSearchParams({
        mode: selection.mode,
        scene: selection.scene,
        caseType: selection.caseType,
        sessionId: session.id,
      })
      navigate(`/device-check?${query.toString()}`)
    },
    onError: async (error) => {
      if (!assessment || !(error instanceof ApiError) || error.status !== 409) return
      setConfigRefreshPending(true)
      try {
        await assessmentConfig.refetch()
      } finally {
        setConfigRefreshPending(false)
      }
    },
  })
  const configConflict = assessment
    && create.error instanceof ApiError
    && create.error.status === 409

  return (
    <main className="archive-page start-page page-enter">
      <p className="eyebrow">{mode === 'assessment' ? 'ASSESSMENT' : 'EXPERIENCE'} / SETUP</p>
      <h1>{mode === 'assessment' ? '正式测评准备' : '自由体验准备'}</h1>
      <p>{assessment
        ? '本次场域和个案类型由管理端统一配置，准备完成后进入对应访谈。'
        : '每次只进入一个场域。系统不会按固定回合截断会谈。'}</p>

      {assessment ? (
        <>
          <div className="field-heading">
            <div>
              <h2>本次测评</h2>
              <p>系统将按当前管理配置随机抽取个案，开始前不会展示来访者的隐藏信息。</p>
            </div>
          </div>
          {configRefreshPending ? (
            <p aria-live="polite">管理配置已更新，正在重新读取</p>
          ) : assessmentConfig.isFetching ? (
            <p aria-live="polite">正在读取本次测评配置…</p>
          ) : assessmentConfig.isError ? (
            <p className="start-error" role="alert">
              本次测评配置读取失败，请刷新页面后重试。
            </p>
          ) : assessmentConfig.data ? (
            <div className="scene-choice-grid scene-choice-grid--pair" aria-label="本次测评配置">
              <article className="scene-choice" style={{ cursor: 'default' }}>
                <Check aria-hidden="true" size={18} />
                <strong>
                  {sceneOptions.find((option) => option.value === assessmentConfig.data.scene)?.name}
                  {' · '}
                  {caseOptions.find((option) => option.value === assessmentConfig.data.case_type)?.name}
                </strong>
                <small>
                  {sceneOptions.find((option) => option.value === assessmentConfig.data.scene)?.media}
                  {' · 个案将在开始时抽取'}
                </small>
              </article>
            </div>
          ) : null}
        </>
      ) : (
        <>
          <div className="field-heading">
            <div>
              <h2>选择场域</h2>
              <p>机构面谈与心理热线为语音，在线咨询为文字。</p>
            </div>
          </div>
          <div className="scene-choice-grid" role="radiogroup" aria-label="选择场域">
            {sceneOptions.map((option) => (
              <label
                className={`scene-choice${option.unavailable ? ' scene-choice--unavailable' : ''}`}
                key={option.value}
              >
                <input
                  type="radio"
                  name="scene"
                  aria-label={option.name}
                  aria-describedby={
                    option.unavailable ? `experience-${option.value}-availability` : undefined
                  }
                  checked={scene === option.value}
                  disabled={option.unavailable || create.isPending}
                  onChange={() => {
                    setScene(option.value)
                    if (option.value === 'online') setCaseType('short')
                  }}
                />
                <span className="scene-choice__check" aria-hidden="true">
                  <Check size={14} />
                </span>
                <strong>{option.name}</strong>
                <small>{option.media}</small>
                {option.unavailable ? (
                  <span
                    className="demo-availability"
                    id={`experience-${option.value}-availability`}
                  >
                    DEMO 暂未开放
                  </span>
                ) : null}
              </label>
            ))}
          </div>

          <div className="field-heading">
            <div>
              <h2>个案类型</h2>
              <p>个案由系统在所选类型中随机抽取。</p>
            </div>
          </div>
          <div className="scene-choice-grid scene-choice-grid--pair" role="radiogroup" aria-label="个案类型">
            {caseOptions.map((option) => {
              const unavailable = scene === 'online' && option.value === 'main'
              return (
                <label
                  className={`scene-choice${unavailable ? ' scene-choice--unavailable' : ''}`}
                  key={option.value}
                >
                  <input
                    type="radio"
                    name="case-type"
                    aria-label={option.name}
                    checked={caseType === option.value}
                    disabled={unavailable || create.isPending}
                    onChange={() => setCaseType(option.value)}
                  />
                  <span className="scene-choice__check" aria-hidden="true">
                    <Check size={14} />
                  </span>
                  <strong>{option.name}</strong>
                  <small>{option.note}</small>
                </label>
              )
            })}
          </div>
        </>
      )}

      {create.isError && !configConflict ? (
        <p className="start-error" role="alert">
          {create.error instanceof Error
            ? create.error.message
            : '会话创建失败，请保留当前选择后重试。'}
        </p>
      ) : null}

      <div className="start-actions">
        <p>下一步是设备检查。</p>
        <button
          className="primary-action"
          type="button"
          disabled={
            create.isPending
            || (assessment && (
              configRefreshPending
              || assessmentConfig.isFetching
              || !assessmentConfig.isSuccess
            ))
          }
          onClick={() => {
            if (!create.isPending) create.mutate()
          }}
        >
          {create.isPending
            ? '正在抽取并建立会话…'
            : assessment ? '开始正式测评' : '抽取个案并开始'}
        </button>
      </div>
    </main>
  )
}
