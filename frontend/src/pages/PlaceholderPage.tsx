import { ArrowLeft, FileClock } from 'lucide-react'
import { Link } from 'react-router-dom'

interface PlaceholderPageProps {
  archiveId: string
  title: string
  description: string
  nextStep: string
}

export function PlaceholderPage({ archiveId, title, description, nextStep }: PlaceholderPageProps) {
  return (
    <section className="placeholder-page page-enter">
      <div className="archive-kicker">档案 {archiveId}</div>
      <FileClock size={28} strokeWidth={1.5} aria-hidden="true" />
      <h1>{title}</h1>
      <p>{description}</p>
      <div className="placeholder-note">
        <span>后续开发</span>
        <strong>{nextStep}</strong>
      </div>
      <Link to="/" className="text-link">
        <ArrowLeft size={16} aria-hidden="true" /> 返回总览
      </Link>
    </section>
  )
}
