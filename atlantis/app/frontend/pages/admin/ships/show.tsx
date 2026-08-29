import type { ReactNode } from 'react'
import { Link } from '@inertiajs/react'
import AdminLayout from '@/layouts/AdminLayout'
import { Badge } from '@/components/admin/ui/badge'
import { Card, CardContent } from '@/components/admin/ui/card'
import HoursDisplay from '@/components/admin/HoursDisplay'
import { CheckIcon, XCircleIcon, ClockIcon, MinusCircleIcon, BanIcon } from 'lucide-react'
import type { AdminShipDetail, SiblingStatuses } from '@/types'

function isSafeUrl(url: string | null): boolean {
  if (!url) return false
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm mt-0.5">{children}</dd>
    </div>
  )
}

const STEPS: { key: keyof SiblingStatuses; label: string; short: string; path: string }[] = [
  { key: 'time_audit', label: 'Time Audit', short: 'TA', path: 'time_audits' },
  { key: 'requirements_check', label: 'Requirements Check', short: 'RC', path: 'requirements_checks' },
  { key: 'design_review', label: 'Design Review', short: 'Design', path: 'design_reviews' },
  { key: 'build_review', label: 'Build Review', short: 'Build', path: 'build_reviews' },
]

function stepIcon(status: string | null) {
  if (!status) return <MinusCircleIcon className="size-4 text-muted-foreground/40" />
  switch (status) {
    case 'approved':
      return <CheckIcon className="size-4 text-emerald-600 dark:text-emerald-400" />
    case 'returned':
    case 'rejected':
      return <XCircleIcon className="size-4 text-red-600 dark:text-red-400" />
    case 'cancelled':
      return <BanIcon className="size-4 text-muted-foreground/50" />
    default:
      return <ClockIcon className="size-4 text-amber-600 dark:text-amber-400" />
  }
}

function stepColor(status: string | null) {
  if (!status) return 'border-border bg-muted/30'
  switch (status) {
    case 'approved':
      return 'border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/50'
    case 'returned':
    case 'rejected':
      return 'border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-950/50'
    case 'cancelled':
      return 'border-border bg-muted/20'
    default:
      return 'border-amber-200 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/50'
  }
}

function ReviewPipeline({ statuses }: { statuses: SiblingStatuses }) {
  return (
    <div className="flex items-stretch gap-1">
      {STEPS.map(({ key, label, short, path }, i) => {
        const { status, id } = statuses[key]
        const inner = (
          <>
            {stepIcon(status)}
            <span className="font-medium hidden sm:inline">{label}</span>
            <span className="font-medium sm:hidden">{short}</span>
            {status && <span className="capitalize text-muted-foreground">{status}</span>}
          </>
        )
        const className = `flex items-center gap-1.5 px-2.5 py-1.5 rounded border text-xs ${stepColor(status)}`
        return (
          <div key={key} className="flex items-center gap-1">
            {i > 0 && <div className="w-3 h-px bg-border shrink-0" />}
            {id ? (
              <Link href={`/admin/reviews/${path}/${id}`} className={`${className} hover:brightness-95 transition`}>
                {inner}
              </Link>
            ) : (
              <div className={className}>{inner}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

const statusColors: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  pending: 'secondary',
  approved: 'default',
  rejected: 'destructive',
  returned: 'outline',
}

export default function AdminShipsShow({ ship }: { ship: AdminShipDetail }) {
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-2xl font-semibold tracking-tight">Ship #{ship.id}</h1>
        <p className="text-sm text-muted-foreground">
          for {ship.project_name} by {ship.user_display_name}
        </p>
      </div>

      <Card>
        <CardContent className="pt-6">
          <dl className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            <Field label="Status">
              <Badge variant={statusColors[ship.status] ?? 'outline'} className="capitalize">
                {ship.status}
              </Badge>
            </Field>
            <Field label="Hours Approved">
              <HoursDisplay publicHours={ship.approved_public_hours} internalHours={ship.approved_internal_hours} />
            </Field>
            <Field label="Created">{ship.created_at}</Field>
          </dl>
        </CardContent>
      </Card>

      <Card className="mt-4">
        <CardContent className="pt-6">
          <p className="text-xs text-muted-foreground mb-3">Review Pipeline</p>
          <ReviewPipeline statuses={ship.review_statuses} />
        </CardContent>
      </Card>

      {(ship.justification || ship.feedback) && (
        <Card className="mt-4">
          <CardContent className="pt-6 space-y-4">
            {ship.justification && <Field label="Justification">{ship.justification}</Field>}
            {ship.feedback && <Field label="Feedback">{ship.feedback}</Field>}
          </CardContent>
        </Card>
      )}

      <Card className="mt-4">
        <CardContent className="pt-6">
          <dl className="grid grid-cols-2 gap-4">
            <Field label="Frozen Demo Link">
              {isSafeUrl(ship.frozen_demo_link) ? (
                <a
                  href={ship.frozen_demo_link!}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary underline truncate block"
                >
                  {ship.frozen_demo_link!.match(/^https?:\/\/github\.com\/([^/]+\/[^/]+)/)?.[1] ??
                    ship.frozen_demo_link}
                </a>
              ) : (
                '—'
              )}
            </Field>
            <Field label="Frozen Repo Link">
              {isSafeUrl(ship.frozen_repo_link) ? (
                <a
                  href={ship.frozen_repo_link!}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary underline truncate block"
                >
                  {ship.frozen_repo_link!.match(/^https?:\/\/github\.com\/([^/]+\/[^/]+)/)?.[1] ??
                    ship.frozen_repo_link}
                </a>
              ) : (
                '—'
              )}
            </Field>
          </dl>
        </CardContent>
      </Card>
    </div>
  )
}

AdminShipsShow.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
