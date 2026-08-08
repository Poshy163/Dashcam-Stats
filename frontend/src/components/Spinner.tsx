import { cn } from '@/lib/cn'

export default function Spinner({
  label,
  className,
}: {
  label?: string
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3', className)} role="status">
      <svg className="h-6 w-6 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2.5" opacity="0.2" />
        <path
          d="M21 12a9 9 0 0 0-9-9"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
        />
      </svg>
      {label && <span className="text-sm text-content-muted">{label}</span>}
    </div>
  )
}
