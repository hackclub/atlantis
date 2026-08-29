import { ChevronDown, ChevronUp, Minus } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/admin/ui/tooltip'
import { Skeleton } from '@/components/admin/ui/skeleton'

export type ReviewStatKey = 'hours_pending' | 'turnaround' | 'approval_ratio' | 'reship_ratio'

export type ReviewStats = {
  hours_pending?: { value: number }
  turnaround?: {
    ship_days: number | null
    cycle_days: number | null
    count: number
    ship_delta: number | null
    cycle_delta: number | null
  }
  approval_ratio?: { percent: number | null; count: number; delta: number | null }
  reship_ratio?: { percent: number | null; count: number; delta: number | null }
}

// delta === null: prior window had no data — render nothing (no signal to compare).
// delta === 0: stats unchanged — render a neutral dash so the reader sees we did
// look and it just hasn't moved (distinct from "no comparison available").
function DeltaChevron({ delta, better, unit }: { delta: number | null; better: 'down' | 'up'; unit: 'd' | '%' }) {
  if (delta == null) return null
  if (delta === 0) {
    return (
      <span className="inline-flex items-center text-xs text-muted-foreground" title="No change vs prior 3d">
        <Minus className="w-3 h-3" />
      </span>
    )
  }
  const up = delta > 0
  const good = (up && better === 'up') || (!up && better === 'down')
  const Icon = up ? ChevronUp : ChevronDown
  const color = good ? 'text-green-700' : 'text-red-700'
  return (
    <span className={`inline-flex items-center text-xs ${color}`} title="vs prior 3d">
      <Icon className="w-3 h-3" />
      {Math.abs(delta).toFixed(1)}
      {unit}
    </span>
  )
}

function StatCard({ label, description, children }: { label: string; description: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border p-3">
      <div className="text-xs text-muted-foreground uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-semibold font-mono mt-1">{children}</div>
      <div className="text-[11px] text-muted-foreground mt-1">{description}</div>
    </div>
  )
}

function fmtDays(d: number | null) {
  return d == null ? '—' : `${d.toFixed(1)}d`
}

function fmtPct(p: number | null) {
  return p == null ? '—' : `${p.toFixed(0)}%`
}

const CARD_META: Record<ReviewStatKey, { label: string; description: string }> = {
  hours_pending: { label: 'Hours pending review', description: 'approved hours waiting in this queue' },
  turnaround: { label: 'P90 Turnaround (3d)', description: '90th-pct wait, includes pending backlog' },
  approval_ratio: { label: 'Approval ratio (3d)', description: 'percentage of ships that get approved' },
  reship_ratio: {
    label: 'Reship ratio (3d)',
    description: 'ships that are re-attempts after a returned/rejected ship',
  },
}

// Each card renderer accepts the (possibly undefined) stat slice. While the deferred `stats`
// payload is still loading (`stats === undefined`) the value slot shows a skeleton sized to the
// text-2xl line, so the card structure is identical before/after the payload lands (no CLS).
// Once loaded, an individual null value renders "—".
function renderCard(key: ReviewStatKey, stats?: ReviewStats, slaDays?: number) {
  if (stats === undefined) {
    // inline-block so the value wrapper's text-2xl line-height (32px) governs the row height —
    // a block skeleton's margins would collapse through the padding-less wrapper and under-size it.
    return (
      <StatCard key={key} {...CARD_META[key]}>
        <Skeleton className="inline-block h-5 w-24 align-middle" />
      </StatCard>
    )
  }

  switch (key) {
    case 'hours_pending': {
      const v = stats?.hours_pending?.value
      return (
        <StatCard key={key} {...CARD_META[key]}>
          {v == null ? '—' : `${v.toFixed(1)}h`}
        </StatCard>
      )
    }
    case 'turnaround': {
      const t = stats?.turnaround
      // Red once the P90 wait reaches the queue's SLA (breach).
      const breached = slaDays != null && t?.ship_days != null && t.ship_days >= slaDays
      return (
        <StatCard key={key} {...CARD_META[key]}>
          <TooltipProvider>
            <span className={breached ? 'text-red-700 dark:text-red-400' : undefined}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="cursor-default">{fmtDays(t?.ship_days ?? null)}</span>
                </TooltipTrigger>
                <TooltipContent>
                  <div>90% of reviews wait less than this, from ship submission.</div>
                  <div>Still-pending reviews count by their current wait, so a backlog shows up here.</div>
                  {t?.cycle_days != null && <div className="mt-1">From cycle start: {t.cycle_days.toFixed(1)}d</div>}
                </TooltipContent>
              </Tooltip>
              {t && (
                <span className="ml-1 align-middle">
                  <DeltaChevron delta={t.ship_delta} better="down" unit="d" />
                </span>
              )}
              {t && (
                <span className="text-muted-foreground text-sm font-normal ml-2">
                  ({t.count} ship{t.count === 1 ? '' : 's'})
                </span>
              )}
            </span>
          </TooltipProvider>
        </StatCard>
      )
    }
    case 'approval_ratio': {
      const a = stats?.approval_ratio
      return (
        <StatCard key={key} {...CARD_META[key]}>
          <span>{fmtPct(a?.percent ?? null)}</span>
          {a && (
            <span className="text-muted-foreground text-sm font-normal ml-2">
              ({a.count} ship{a.count === 1 ? '' : 's'})
            </span>
          )}
          {a && (
            <span className="ml-2 align-middle">
              <DeltaChevron delta={a.delta} better="up" unit="%" />
            </span>
          )}
        </StatCard>
      )
    }
    case 'reship_ratio': {
      const r = stats?.reship_ratio
      return (
        <StatCard key={key} {...CARD_META[key]}>
          <span>{fmtPct(r?.percent ?? null)}</span>
          {r && (
            <span className="text-muted-foreground text-sm font-normal ml-2">
              ({r.count} ship{r.count === 1 ? '' : 's'})
            </span>
          )}
          {r && (
            <span className="ml-2 align-middle">
              <DeltaChevron delta={r.delta} better="down" unit="%" />
            </span>
          )}
        </StatCard>
      )
    }
  }
}

export function ReviewStatsHeader({
  stats_keys,
  stats,
  sla_days,
}: {
  stats_keys: ReviewStatKey[]
  stats?: ReviewStats
  sla_days?: number
}) {
  if (stats_keys.length === 0) return null

  // Tailwind JIT can't compile interpolated class names, so map count → literal classes.
  const gridClass =
    stats_keys.length === 1 ? 'sm:grid-cols-1' : stats_keys.length === 2 ? 'sm:grid-cols-2' : 'sm:grid-cols-3'

  return (
    <div className={`grid grid-cols-1 ${gridClass} gap-3 mb-4`}>
      {stats_keys.map((key) => renderCard(key, stats, sla_days))}
    </div>
  )
}
