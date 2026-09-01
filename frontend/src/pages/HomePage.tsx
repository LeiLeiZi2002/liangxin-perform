import {
  ArrowUpRight,
  Building2,
  ClipboardCheck,
  Headphones,
  MessageSquareText,
  Settings2,
  Sparkles,
  type LucideIcon,
} from 'lucide-react'
import { Link } from 'react-router-dom'

interface Entry {
  index: string
  title: string
  eyebrow: string
  description: string
  to: string
  icon: LucideIcon
  accent?: boolean
}

const entries: Entry[] = [
  {
    index: '01',
    title: '正式测评',
    eyebrow: '完整流程',
    description: '抽取个案，完成访谈与工作记录，形成可追溯到原话的测评材料。',
    to: '/assessment',
    icon: ClipboardCheck,
    accent: true,
  },
  {
    index: '02',
    title: '自由体验',
    eyebrow: '不计分练习',
    description: '自选场域和个案，只谈不评分，用来熟悉来访者的反应方式。',
    to: '/experience',
    icon: Sparkles,
  },
  {
    index: '03',
    title: '任务配置',
    eyebrow: '管理员',
    description: '设定场域、个案类型与任务数量，并填写模型服务密钥。',
    to: '/configure',
    icon: Settings2,
  },
]

const features = [
  {
    title: '事实由专家审定，不由模型发挥',
    body: '来访者的核心经历、风险事实与伦理冲突事先审定，不会因为模型自由生成而改变；情绪强度、合作程度和披露节奏才按你的提问在预设规则内变化。',
  },
  {
    title: '没机会展现，不算能力缺失',
    body: '每个维度区分「已经观察到」「任务提供了机会但未表现」和「本任务没有充分机会观察」，不把没有出现的情境误判成不会。',
  },
  {
    title: '四类证据，逐条锚定原话',
    body: '会谈中的可观察行为、任务产出、过程证据与安全证据分开采集，每一项分数都指回具体的原话和回合。',
  },
  {
    title: 'AI 不下最终结论',
    body: 'AI 只负责呈现情境、记录过程和提取候选证据，安全标记与最终分数由人工复核决定，改分轨迹完整保留。',
  },
]

const scenes = [
  {
    title: '机构面谈',
    media: '实时语音',
    description: '线索最完整的场域，需要展示较完整的问题评估、个案理解与行动决策。',
    icon: Building2,
    unavailable: true,
  },
  {
    title: '心理热线',
    media: '实时语音',
    description: '只能靠声音判断，侧重即时稳定、风险识别与适当转介。',
    icon: Headphones,
    unavailable: false,
  },
  {
    title: '在线咨询',
    media: '实时文字',
    description: '文字往来、节奏更慢，侧重澄清提问、专业边界与转介衔接。',
    icon: MessageSquareText,
    unavailable: false,
  },
]

export function HomePage() {
  return (
    <div className="home-page page-enter">
      <section className="hero-sheet" aria-labelledby="home-title">
        <div className="hero-sheet__copy">
          <h1 id="home-title">
            初阶心理服务从业者 · 胜任力测评
          </h1>
          <p className="hero-sheet__lead">
            你将扮演心理工作者，在一个会随你提问而变化的助人情境中实际作答；系统据此判断你能否建立关系、理解来访者的处境、选择适当行动，并安全、合乎伦理地处理风险与专业边界。
          </p>
          <div className="hero-actions">
            <Link className="button button--coral" to="/assessment">
              开始正式测评
              <ArrowUpRight size={18} aria-hidden="true" />
            </Link>
            <Link className="text-link" to="/experience">
              先不计分体验一次
              <ArrowUpRight size={16} aria-hidden="true" />
            </Link>
          </div>
        </div>
      </section>

      <section className="feature-section" aria-label="系统特点">
        <div className="feature-grid">
          {features.map((item) => (
            <article className="feature-item" key={item.title}>
              <h3>{item.title}</h3>
              <p>{item.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="entry-section" aria-label="演示入口">
        <header className="section-heading">
          <div>
            <p className="archive-kicker">选择工作入口</p>
            <h2 id="entry-title">三个入口，覆盖一次完整流程</h2>
          </div>
          <p>演示阶段不需要登录，也不需要创建机构账号。</p>
        </header>

        <div className="entry-grid">
          {entries.map((entry) => {
            const Icon = entry.icon
            return (
              <Link
                key={entry.to}
                to={entry.to}
                className={`entry-card${entry.accent ? ' entry-card--accent' : ''}`}
              >
                <span className="entry-card__index">{entry.index}</span>
                <Icon size={24} strokeWidth={1.5} aria-hidden="true" />
                <span className="entry-card__eyebrow">{entry.eyebrow}</span>
                <h3>{entry.title}</h3>
                <p>{entry.description}</p>
                <span className="entry-card__action">
                  进入 <ArrowUpRight size={17} aria-hidden="true" />
                </span>
              </Link>
            )
          })}
        </div>
      </section>

      <section className="scene-section" aria-label="测评场域">
        <header className="section-heading section-heading--compact">
          <div>
            <p className="archive-kicker">三个独立场域</p>
            <h2 id="scene-title">三个场域，同一套能力框架</h2>
          </div>
          <p>一次只进入一个场域，个案不设置固定回合数，媒介和考察重点各不相同。</p>
        </header>

        <div className="scene-ledger">
          {scenes.map((scene) => {
            const Icon = scene.icon
            return (
              <article
                className={`scene-row${scene.unavailable ? ' scene-row--unavailable' : ''}`}
                key={scene.title}
              >
                <span className="scene-row__icon">
                  <Icon size={22} strokeWidth={1.5} aria-hidden="true" />
                </span>
                <div>
                  <div className="scene-row__heading">
                    <h3>{scene.title}</h3>
                    {scene.unavailable ? (
                      <span className="demo-availability">DEMO 暂未开放</span>
                    ) : null}
                  </div>
                  <p>{scene.description}</p>
                </div>
                <strong className="scene-row__media">{scene.media}</strong>
              </article>
            )
          })}
        </div>
      </section>

      <section className="positioning" aria-labelledby="positioning-title">
        <div>
          <p className="archive-kicker">项目定位</p>
          <h2 id="positioning-title">一条能够说明、复核和检验的测评证据链</h2>
        </div>
        <div className="positioning__body">
          <p>
            面向已接受心理咨询或心理支持基础训练、正在进入实践与实习阶段，仍需在督导或机构质量保障下开展工作的人员。系统不以知识记忆、自我评价或单次督导印象替代能力判断。
          </p>
          <p>
            测评回答四件事：评什么、经由什么表现能够说明具备该能力、什么任务能够引出这些表现，以及分数如何解释。能力分为共同助人核心、心理咨询专门能力与安全伦理底线三层，安全与伦理单独报告，不被沟通技能的高分抵消。
          </p>
          <p className="positioning__note">
            AI 承担情境呈现、过程记录与证据提取，不承担未经验证的独立职业资格裁决；最终分数以人工复核为准。
          </p>
        </div>
      </section>
    </div>
  )
}
