import { ArrowLeft, Check } from 'lucide-react'
import { Link, useLocation, useParams } from 'react-router-dom'

export function CompletionPage() {
  const { sessionId = '' } = useParams()
  const location = useLocation()
  const submitted = (location.state as { workRecordSubmitted?: boolean } | null)
    ?.workRecordSubmitted === true

  return (
    <main className="completion-page page-enter">
      <section className="completion-sheet">
        <div className="completion-mark" aria-hidden="true">
          <Check size={34} strokeWidth={1.7} />
        </div>
        <p className="eyebrow">ASSESSMENT COMPLETE</p>
        <h1>{submitted ? '本次测评已完成' : '本次测评流程已结束'}</h1>
        <p className="completion-lead">{submitted
          ? '热线工作记录已经提交，本次操作到这里结束。'
          : '当前页面没有工作记录的提交确认，请返回会谈记录核对。'}</p>
        <div className="completion-note">
          <span>记录状态</span>
          <strong>{submitted ? '已保存' : '待确认'}</strong>
          <p>{submitted
            ? '你可以返回总览，或重新开始一次正式测评。'
            : '返回本次会谈，可继续进入工作记录。'}</p>
        </div>
        <div className="completion-actions">
          <Link className="button button--coral" to={submitted ? '/assessment' : `/session/${sessionId}`}>
            {submitted ? '重新开始测评' : '返回会谈记录'}
          </Link>
          <Link className="text-link" to="/">
            <ArrowLeft size={15} aria-hidden="true" />
            返回总览
          </Link>
        </div>
      </section>
    </main>
  )
}
