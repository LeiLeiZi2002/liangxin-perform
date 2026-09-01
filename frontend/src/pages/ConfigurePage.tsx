import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, Check, RotateCcw, Save, Settings2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import {
  ApiError,
  getDemoConfig,
  getProviderConfig,
  updateDemoConfig,
  updateProviderConfig,
} from '../api/client'
import type { DemoConfig, ProviderConfig, ProviderConfigUpdate, Scene } from '../api/contracts'

interface FormState {
  scene: Scene
  caseType: DemoConfig['case_type']
  requireWorkRecord: boolean
}

interface ProviderFormState {
  apiKey: string
  workspaceId: string
  reportModel: string
  actorModel: string
  asrModel: string
  ttsModel: string
  ttsVoice: string
  reportTemperature: number
  actorTemperature: number
  actorContextWindowTokens: number | ''
  actorMaxOutputTokens: number
}

const knownActorModelDefaults: Record<string, {
  contextWindowTokens: number
  maxOutputTokens: number
}> = {
  'qwen-plus-character': {
    contextWindowTokens: 32768,
    maxOutputTokens: 2048,
  },
}

const sceneOptions: Array<{
  value: Scene
  name: string
  media: string
  unavailable: boolean
}> = [
  { value: 'institution', name: '机构面谈', media: '实时语音', unavailable: true },
  { value: 'hotline', name: '心理热线', media: '实时语音', unavailable: false },
  { value: 'online', name: '在线咨询', media: '实时文字', unavailable: false },
]

function toFormState(config: DemoConfig): FormState {
  const scene = sceneOptions.some(
    (option) => option.value === config.scene && !option.unavailable,
  ) ? config.scene : 'hotline'
  return {
    scene,
    caseType: config.case_type,
    requireWorkRecord: config.require_work_record,
  }
}

function toProviderFormState(config: ProviderConfig): ProviderFormState {
  return {
    apiKey: '',
    workspaceId: config.workspace_id ?? '',
    reportModel: config.report_model,
    actorModel: config.actor_model,
    asrModel: config.asr_model,
    ttsModel: config.tts_model,
    ttsVoice: config.tts_voice,
    reportTemperature: config.report_temperature,
    actorTemperature: config.actor_temperature,
    actorContextWindowTokens: config.actor_context_window_tokens,
    actorMaxOutputTokens: config.actor_max_output_tokens,
  }
}

function toProviderPayload(form: ProviderFormState): ProviderConfigUpdate {
  return {
    api_key: form.apiKey,
    workspace_id: form.workspaceId.trim() || null,
    report_model: form.reportModel.trim(),
    actor_model: form.actorModel.trim(),
    asr_model: form.asrModel.trim(),
    tts_model: form.ttsModel.trim(),
    tts_voice: form.ttsVoice.trim(),
    report_temperature: form.reportTemperature,
    actor_temperature: form.actorTemperature,
    actor_context_window_tokens: Number(form.actorContextWindowTokens),
    actor_max_output_tokens: form.actorMaxOutputTokens,
  }
}

function messageFrom(error: unknown): string {
  return error instanceof ApiError ? error.message : '操作没有完成，请稍后重试。'
}

function toDemoConfigPayload(form: FormState): DemoConfig {
  return {
    scene: form.scene,
    case_type: form.caseType,
    task_count: 1,
    soft_duration_minutes: null,
    model_mode: 'live',
    require_work_record: form.requireWorkRecord,
  }
}

export function ConfigurePage() {
  const queryClient = useQueryClient()
  const config = useQuery({ queryKey: ['demo-config'], queryFn: getDemoConfig })
  const providerConfig = useQuery({ queryKey: ['provider-config'], queryFn: getProviderConfig })
  const [draft, setForm] = useState<FormState | null>(null)
  const [providerDraft, setProviderForm] = useState<ProviderFormState | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [providerNotice, setProviderNotice] = useState<string | null>(null)
  const [providerFormError, setProviderFormError] = useState<string | null>(null)
  const form = draft ?? (config.data ? toFormState(config.data) : null)
  const providerForm = providerDraft ?? (
    providerConfig.data ? toProviderFormState(providerConfig.data) : null
  )

  const save = useMutation({
    mutationFn: updateDemoConfig,
    onSuccess: (saved) => {
      queryClient.setQueryData(['demo-config'], saved)
      setForm(toFormState(saved))
      setNotice('配置已保存，仅对新会话生效。')
    },
    onError: () => setNotice(null),
  })

  const saveProvider = useMutation({
    mutationFn: updateProviderConfig,
    onSuccess: (saved) => {
      queryClient.setQueryData(['provider-config'], saved)
      setProviderForm(toProviderFormState(saved))
      setProviderNotice('模型与语音服务配置已保存。')
    },
    onError: () => setProviderNotice(null),
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!form) return
    setNotice(null)
    save.mutate(toDemoConfigPayload(form))
  }

  function submitProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!providerForm) return
    setProviderNotice(null)
    const contextWindowTokens = providerForm.actorContextWindowTokens
    if (
      contextWindowTokens === ''
      || !Number.isInteger(contextWindowTokens)
      || contextWindowTokens <= 0
    ) {
      const actorModel = providerForm.actorModel.trim()
      const savedActorModel = providerConfig.data?.actor_model
      setProviderFormError(
        actorModel !== savedActorModel && knownActorModelDefaults[actorModel] === undefined
          ? '换用未知来访者对话模型时，请填写模型官方上下文容量。'
          : '请填写正整数的对话模型上下文容量。',
      )
      return
    }
    if (
      !Number.isInteger(providerForm.actorMaxOutputTokens)
      || providerForm.actorMaxOutputTokens <= 0
    ) {
      setProviderFormError('请填写正整数的单次回复输出上限。')
      return
    }
    setProviderFormError(null)
    saveProvider.mutate(toProviderPayload(providerForm))
  }

  if (config.isPending && !form) {
    return (
      <section className="loading-sheet page-enter" aria-live="polite">
        <span className="loading-sheet__mark" aria-hidden="true" />
        <p>正在读取任务配置…</p>
        <small>连接本地演示服务</small>
      </section>
    )
  }

  if (config.isError && !form) {
    return (
      <section className="error-sheet page-enter">
        <AlertCircle size={27} strokeWidth={1.5} aria-hidden="true" />
        <h1>任务配置暂时无法读取</h1>
        <p role="alert">{messageFrom(config.error)}</p>
        <button type="button" className="button button--ink" onClick={() => void config.refetch()}>
          <RotateCcw size={16} aria-hidden="true" /> 重新读取
        </button>
      </section>
    )
  }

  if (!form) return null

  return (
    <div className="configure-page page-enter">
      <header className="page-heading">
        <div>
          <p className="archive-kicker">管理员</p>
          <h1>任务配置</h1>
          <p>配置只作用于新建会话，不会改变已经开始的访谈。</p>
        </div>
        <Settings2 size={32} strokeWidth={1.35} aria-hidden="true" />
      </header>

      <form className="config-form" onSubmit={submit} noValidate>
        <fieldset className="config-form__lock" disabled={save.isPending}>
        <section className="form-section" aria-labelledby="scene-legend">
          <div className="form-section__index">01</div>
          <div className="form-section__content">
            <div className="field-heading">
              <div>
                <h2 id="scene-legend">指定测评场域</h2>
                <p>每个任务只进入一个场域。</p>
              </div>
              <span>必选</span>
            </div>
            <div
              className="scene-choice-grid"
              role="radiogroup"
              aria-labelledby="scene-legend"
            >
              {sceneOptions.map((scene) => {
                const nameId = `configure-${scene.value}-name`
                const mediaId = `configure-${scene.value}-media`
                const availabilityId = `configure-${scene.value}-availability`
                return (
                  <label
                    className={`scene-choice${scene.unavailable ? ' scene-choice--unavailable' : ''}`}
                    key={scene.value}
                  >
                  <input
                    type="radio"
                    name="scene"
                    value={scene.value}
                    aria-labelledby={nameId}
                    aria-describedby={`${mediaId}${scene.unavailable ? ` ${availabilityId}` : ''}`}
                    checked={form.scene === scene.value}
                    disabled={scene.unavailable}
                    onChange={() => setForm({ ...form, scene: scene.value })}
                  />
                  <span className="scene-choice__check" aria-hidden="true">
                    <Check size={14} />
                  </span>
                  <strong id={nameId}>{scene.name}</strong>
                  <small id={mediaId}>{scene.media}</small>
                  {scene.unavailable ? (
                    <span
                      className="demo-availability"
                      id={availabilityId}
                    >
                      DEMO 暂未开放
                    </span>
                  ) : null}
                  </label>
                )
              })}
            </div>
          </div>
        </section>

        <section className="form-section" aria-labelledby="task-options-title">
          <div className="form-section__index">02</div>
          <div className="form-section__content">
            <div className="field-heading">
              <div>
                <h2 id="task-options-title">组卷设置</h2>
                <p>选择本次测评使用的个案类型。</p>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>个案类型</span>
                <select
                  value={form.caseType}
                  onChange={(event) =>
                    setForm({ ...form, caseType: event.target.value as DemoConfig['case_type'] })
                  }
                >
                  <option value="main">主个案</option>
                  <option value="short">短个案</option>
                </select>
              </label>
              <label className="field">
                <span>任务数量</span>
                <input
                  type="number"
                  aria-label="任务数量"
                  min="1"
                  step="1"
                  value={1}
                  aria-describedby="task-count-help"
                  disabled
                  readOnly
                />
                <small id="task-count-help">当前演示每次安排一份任务。</small>
              </label>
            </div>
          </div>
        </section>

        <section className="form-section" aria-labelledby="runtime-title">
          <div className="form-section__index">03</div>
          <div className="form-section__content">
            <div className="field-heading">
              <div>
                <h2 id="runtime-title">运行与记录</h2>
                <p>测评使用实时模型链路；发生调用异常时暂停会谈并保留已经完成的记录。</p>
              </div>
            </div>
            <div className="form-grid form-grid--runtime">
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={form.requireWorkRecord}
                  onChange={(event) =>
                    setForm({ ...form, requireWorkRecord: event.target.checked })
                  }
                />
                <span aria-hidden="true">
                  <Check size={14} />
                </span>
                <strong>必须填写工作记录</strong>
                <small>正式测评建议保持开启，以生成完整证据报告。</small>
              </label>
            </div>
          </div>
        </section>

        <footer className="form-actions">
          <div aria-live="polite">
            {notice && <p className="success-message">{notice}</p>}
            {save.isError && <p role="alert">{messageFrom(save.error)}</p>}
          </div>
          <button type="submit" className="button button--coral" disabled={save.isPending}>
            <Save size={17} aria-hidden="true" />
            {save.isPending ? '正在保存…' : '保存配置'}
          </button>
        </footer>
        </fieldset>
      </form>

      <form className="config-form provider-config-form" onSubmit={submitProvider} noValidate>
        <fieldset className="config-form__lock" disabled={saveProvider.isPending}>
        <section className="form-section" aria-labelledby="provider-config-title">
          <div className="form-section__index">04</div>
          <div className="form-section__content">
            <div className="field-heading">
              <div>
                <h2 id="provider-config-title">模型与语音服务</h2>
                <p>启动器会优先载入本机用户环境中的密钥；在此填写的密钥只保留到当前后端进程结束。</p>
              </div>
              {providerConfig.data?.configured ? (
                <span>已配置 · 末四位 {providerConfig.data.masked_key?.replace('••••', '')}</span>
              ) : (
                <span>尚未配置</span>
              )}
            </div>

            {providerConfig.isPending && !providerForm ? (
              <p className="provider-config__loading">正在读取模型服务配置…</p>
            ) : providerConfig.isError && !providerForm ? (
              <p className="field-error" role="alert">{messageFrom(providerConfig.error)}</p>
            ) : providerForm ? (
              <>
                <div className="form-grid form-grid--provider">
                  <label className="field">
                    <span>百炼 API Key</span>
                    <input
                      aria-label="百炼 API Key"
                      type="password"
                      autoComplete="new-password"
                      placeholder={providerConfig.data?.configured ? '留空则保留当前密钥' : '粘贴百炼 API Key'}
                      value={providerForm.apiKey}
                      onChange={(event) => setProviderForm({ ...providerForm, apiKey: event.target.value })}
                    />
                    <small>不会写入浏览器、本地存储、数据库或日志。</small>
                  </label>
                  <label className="field">
                    <span>业务空间标识（可选）</span>
                    <input
                      aria-label="业务空间标识（可选）"
                      autoComplete="off"
                      placeholder="留空使用百炼公共地址"
                      value={providerForm.workspaceId}
                      onChange={(event) => setProviderForm({ ...providerForm, workspaceId: event.target.value })}
                    />
                    <small>填写后使用华北地区业务空间专属地址。</small>
                  </label>
                </div>

                <details className="advanced-settings">
                  <summary>高级设置</summary>
                  <div className="advanced-settings__content">
                    <div className="form-grid form-grid--advanced">
                      <label className="field">
                        <span>报告分析模型</span>
                        <input
                          aria-label="报告分析模型"
                          value={providerForm.reportModel}
                          onChange={(event) => setProviderForm({ ...providerForm, reportModel: event.target.value })}
                        />
                      </label>
                      <label className="field">
                        <span>来访者对话模型</span>
                        <input
                          aria-label="来访者对话模型"
                          value={providerForm.actorModel}
                          onChange={(event) => {
                            const actorModel = event.target.value
                            const knownDefaults = knownActorModelDefaults[actorModel]
                            const savedActorModel = providerConfig.data?.actor_model
                            setProviderFormError(null)
                            setProviderForm({
                              ...providerForm,
                              actorModel,
                              actorContextWindowTokens: (
                                actorModel === savedActorModel
                                  ? providerConfig.data?.actor_context_window_tokens
                                    ?? providerForm.actorContextWindowTokens
                                  : knownDefaults?.contextWindowTokens ?? ''
                              ),
                              actorMaxOutputTokens: (
                                actorModel === savedActorModel
                                  ? providerConfig.data?.actor_max_output_tokens
                                    ?? providerForm.actorMaxOutputTokens
                                  : knownDefaults?.maxOutputTokens
                                    ?? providerForm.actorMaxOutputTokens
                              ),
                            })
                          }}
                        />
                      </label>
                      <label className="field">
                        <span>实时语音识别模型</span>
                        <input value={providerForm.asrModel} onChange={(event) => setProviderForm({ ...providerForm, asrModel: event.target.value })} />
                      </label>
                      <label className="field">
                        <span>流式语音合成模型</span>
                        <input value={providerForm.ttsModel} onChange={(event) => setProviderForm({ ...providerForm, ttsModel: event.target.value })} />
                      </label>
                      <label className="field">
                        <span>音色</span>
                        <input value={providerForm.ttsVoice} onChange={(event) => setProviderForm({ ...providerForm, ttsVoice: event.target.value })} />
                      </label>
                      <label className="field">
                        <span>报告分析温度</span>
                        <input
                          aria-label="报告分析温度"
                          type="number"
                          min="0"
                          max="2"
                          step="0.05"
                          value={providerForm.reportTemperature}
                          onChange={(event) => setProviderForm({ ...providerForm, reportTemperature: Number(event.target.value) })}
                        />
                      </label>
                      <label className="field">
                        <span>来访者对话温度</span>
                        <input
                          aria-label="来访者对话温度"
                          type="number"
                          min="0"
                          max="2"
                          step="0.05"
                          value={providerForm.actorTemperature}
                          onChange={(event) => setProviderForm({ ...providerForm, actorTemperature: Number(event.target.value) })}
                        />
                      </label>
                      <label className="field">
                        <span>对话模型上下文容量</span>
                        <input
                          aria-label="对话模型上下文容量"
                          type="number"
                          min="1"
                          step="1"
                          value={providerForm.actorContextWindowTokens}
                          onChange={(event) => {
                            setProviderFormError(null)
                            setProviderForm({
                              ...providerForm,
                              actorContextWindowTokens: (
                                event.target.value === '' ? '' : Number(event.target.value)
                              ),
                            })
                          }}
                        />
                        <small>容量接近上限时，来访者会先聚焦当前话题，再自然收束会话。</small>
                      </label>
                      <label className="field">
                        <span>单次回复输出上限</span>
                        <input
                          aria-label="单次回复输出上限"
                          type="number"
                          min="1"
                          step="1"
                          value={providerForm.actorMaxOutputTokens}
                          onChange={(event) => setProviderForm({ ...providerForm, actorMaxOutputTokens: Number(event.target.value) })}
                        />
                      </label>
                    </div>
                  </div>
                </details>
              </>
            ) : null}
          </div>
        </section>
        <footer className="form-actions">
          <div aria-live="polite">
            {providerNotice && <p className="success-message">{providerNotice}</p>}
            {providerFormError && <p role="alert">{providerFormError}</p>}
            {saveProvider.isError && <p role="alert">{messageFrom(saveProvider.error)}</p>}
          </div>
          <button type="submit" className="button button--coral" disabled={!providerForm || saveProvider.isPending}>
            <Save size={17} aria-hidden="true" />
            {saveProvider.isPending ? '正在保存…' : '保存服务配置'}
          </button>
        </footer>
        </fieldset>
      </form>
    </div>
  )
}
