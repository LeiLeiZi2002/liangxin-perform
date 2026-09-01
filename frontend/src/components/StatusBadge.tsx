import clsx from 'clsx'

type StatusTone = 'ready' | 'waiting' | 'offline' | 'neutral'

interface StatusBadgeProps {
  label: string
  value: string
  tone?: StatusTone
}

export function StatusBadge({ label, value, tone = 'neutral' }: StatusBadgeProps) {
  return (
    <span className={clsx('status-badge', `status-badge--${tone}`)}>
      <span className="status-badge__dot" aria-hidden="true" />
      <span className="status-badge__label">{label}</span>
      <span>{value}</span>
    </span>
  )
}
