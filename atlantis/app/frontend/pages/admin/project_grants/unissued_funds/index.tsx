import type { ReactNode } from 'react'
import { useState } from 'react'
import { Link, router } from '@inertiajs/react'
import { Coins, Fish, Send } from 'lucide-react'
import AdminLayout from '@/layouts/AdminLayout'
import { Badge } from '@/components/admin/ui/badge'
import { Button } from '@/components/admin/ui/button'
import TimeAgo from '@/components/shared/TimeAgo'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/admin/ui/alert-dialog'

type Cause = 'card_closed' | 'manual_out' | 'failed_topup' | 'pending_topup' | 'unknown'

type Row = {
  user: { id: number; display_name: string; email: string; avatar: string }
  owed_cents: number
  expected_cents: number
  transferred_cents: number
  has_active_card: boolean
  has_card: boolean
  has_pending_topup: boolean
  gold_refundable: number
  cause: Cause
  owed_since: string | null
  last_fulfilled_order_at: string | null
}

type Rates = { koi_to_cents_numerator: number; koi_to_cents_denominator: number }

const CAUSE_LABELS: Record<Cause, { label: string; hint: string }> = {
  card_closed: {
    label: 'Card closed',
    hint: 'Their card was canceled or expired and the unspent balance went back to the org. Fallout auto-booked an out row that replenished their funding — it just needs an order to go out on.',
  },
  manual_out: {
    label: 'Manual adjustment',
    hint: 'An admin booked an out adjustment counting toward funding, which raised what we owe.',
  },
  failed_topup: {
    label: 'Failed topup',
    hint: 'A settle attempt failed, so the money never reached the card. This is reconciliation work, not just waiting on an order — check the warnings table.',
  },
  pending_topup: {
    label: 'Pending topup',
    hint: 'A topup row is still pending. Settling is blocked until it is reconciled — the delta will not clear on its own.',
  },
  unknown: {
    label: 'Unknown',
    hint: 'No out row, failed topup or pending topup explains this delta. Worth investigating on the user page.',
  },
}

function formatDollars(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`
}

// Mirrors HcbGrantSetting#refund_units_for_usd_cents — floor, so the preview never
// promises more currency than the server will actually mint. The server recomputes
// this on submit; nothing here is trusted.
function splitRefund(cents: number, rates: Rates, goldRefundable: number) {
  const units = Math.floor((cents * rates.koi_to_cents_denominator) / rates.koi_to_cents_numerator)
  const gold = Math.min(units, goldRefundable)
  return { units, gold, koi: units - gold }
}

export default function AdminProjectGrantsUnissuedFunds({
  rows,
  stats,
  truncated,
  row_limit,
  rates,
}: {
  rows: Row[]
  stats: { user_count: number; total_owed_cents: number }
  truncated: boolean
  row_limit: number
  rates: Rates
}) {
  const [refundRow, setRefundRow] = useState<Row | null>(null)
  const [refundAmount, setRefundAmount] = useState('')
  const [issueRow, setIssueRow] = useState<Row | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function openRefund(row: Row) {
    setRefundRow(row)
    setRefundAmount((row.owed_cents / 100).toFixed(2))
  }

  function submitRefund() {
    if (!refundRow) return
    setSubmitting(true)
    router.post(
      `/admin/project_grants/unissued_funds/${refundRow.user.id}/refund_to_currency`,
      { amount_dollars: refundAmount },
      {
        onFinish: () => {
          setSubmitting(false)
          setRefundRow(null)
        },
      },
    )
  }

  function submitIssue() {
    if (!issueRow) return
    setSubmitting(true)
    router.post(
      `/admin/project_grants/unissued_funds/${issueRow.user.id}/issue_funds`,
      {},
      {
        onFinish: () => {
          setSubmitting(false)
          setIssueRow(null)
        },
      },
    )
  }

  const refundCents = Math.round((parseFloat(refundAmount) || 0) * 100)
  const preview = refundRow ? splitRefund(refundCents, rates, refundRow.gold_refundable) : null
  const refundValid =
    refundRow != null && refundCents > 0 && refundCents <= refundRow.owed_cents && (preview?.units ?? 0) > 0

  return (
    <div>
      <div className="mb-4">
        <Link href="/admin/project_grants/orders" className="text-sm text-primary hover:underline">
          ← Project Grants
        </Link>
      </div>

      <h1 className="text-2xl font-semibold tracking-tight mb-2">Unissued funds</h1>
      <p className="text-sm text-muted-foreground mb-4 max-w-3xl">
        Users whose fulfilled orders total more than what's actually been transferred to them, with no order awaiting
        fulfilment to deliver the difference. We owe them this balance, but nothing can push it out — their next card
        will simply be topped up by more than they asked for. Users with an order still in the queue are excluded: that
        order already settles the whole delta when you fulfil it.
      </p>

      <div className="grid grid-cols-2 gap-3 mb-6 max-w-md">
        <div className="rounded-md border border-border p-3">
          <div className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">Users owed</div>
          <div className="text-xl font-semibold tabular-nums">{stats.user_count}</div>
        </div>
        <div className="rounded-md border border-border p-3">
          <div className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">Total owed</div>
          <div className="text-xl font-semibold tabular-nums">{formatDollars(stats.total_owed_cents)}</div>
        </div>
      </div>

      {truncated && (
        <p className="mb-3 text-xs text-muted-foreground">
          Showing the {row_limit} largest balances of {stats.user_count}. The totals above cover everyone.
        </p>
      )}

      <div className="rounded-md border border-border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 border-b border-border">
            <tr className="text-left">
              <th className="p-3">User</th>
              <th className="p-3" title="Fulfilled orders minus funding actually transferred">
                Owed
              </th>
              <th className="p-3" title="Sum of frozen_usd_cents across fulfilled orders">
                Expected
              </th>
              <th className="p-3" title="Completed topups counting toward funding, in minus out">
                Transferred
              </th>
              <th className="p-3">Card</th>
              <th className="p-3">Why</th>
              <th className="p-3">Owed since</th>
              <th className="p-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="p-6 text-center text-muted-foreground">
                  Nobody is owed funding without a way to deliver it.
                </td>
              </tr>
            ) : (
              rows.map((row) => {
                const cause = CAUSE_LABELS[row.cause]
                // A pending topup may or may not have reached HCB, so neither action is
                // safe until it's reconciled — the same state settle! refuses to run in.
                const blockedReason = row.has_pending_topup
                  ? 'Pending topup — reconcile it on the order page before moving this balance.'
                  : null
                const refundBlockedReason =
                  blockedReason ?? (row.has_card ? null : 'No HCB grant card to book the ledger entry against.')
                return (
                  <tr key={row.user.id} className="border-b border-border last:border-0 hover:bg-muted/30">
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <img src={row.user.avatar} alt="" className="size-7 rounded-full object-cover" loading="lazy" />
                        <div className="min-w-0">
                          <Link href={`/admin/users/${row.user.id}`} className="font-medium hover:underline">
                            {row.user.display_name}
                          </Link>
                          <div className="truncate text-xs text-muted-foreground">{row.user.email}</div>
                        </div>
                      </div>
                    </td>
                    <td className="p-3 font-mono font-semibold tabular-nums">{formatDollars(row.owed_cents)}</td>
                    <td className="p-3 font-mono tabular-nums text-muted-foreground">
                      {formatDollars(row.expected_cents)}
                    </td>
                    <td className="p-3 font-mono tabular-nums text-muted-foreground">
                      {formatDollars(row.transferred_cents)}
                    </td>
                    <td className="p-3">
                      {row.has_active_card ? (
                        <span className="text-xs text-muted-foreground">Active</span>
                      ) : (
                        <Badge variant="outline" className="font-normal" title="Their next order issues a new card">
                          None
                        </Badge>
                      )}
                    </td>
                    <td className="p-3">
                      <Badge variant="outline" className="font-normal" title={cause.hint}>
                        {cause.label}
                      </Badge>
                    </td>
                    <td className="p-3 text-xs text-muted-foreground">
                      {row.owed_since ? (
                        <TimeAgo datetime={row.owed_since} />
                      ) : row.last_fulfilled_order_at ? (
                        <span title="No out row — dated from their last fulfilled order">
                          <TimeAgo datetime={row.last_fulfilled_order_at} />
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="p-3">
                      <div className="flex justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={refundBlockedReason != null}
                          title={refundBlockedReason ?? 'Convert this balance back into koi/gold'}
                          onClick={() => openRefund(row)}
                        >
                          <Coins className="w-4 h-4 mr-1" /> Refund to koi/gold
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={blockedReason != null}
                          title={blockedReason ?? "Send this balance to the user's card"}
                          onClick={() => setIssueRow(row)}
                        >
                          <Send className="w-4 h-4 mr-1" /> Issue funds
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      <AlertDialog open={refundRow != null} onOpenChange={(open) => !open && setRefundRow(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Refund {refundRow?.user.display_name} to koi/gold?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-3">
                <p>
                  This converts owed funding back into currency. No money moves on HCB — the dollars are already back at
                  the org, which is why they're owed. The balance stops being spendable funding.
                </p>

                <label className="block">
                  <span className="block text-xs font-medium mb-1 text-foreground">
                    Amount to refund (max {formatDollars(refundRow?.owed_cents ?? 0)})
                  </span>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    max={((refundRow?.owed_cents ?? 0) / 100).toFixed(2)}
                    value={refundAmount}
                    onChange={(e) => setRefundAmount(e.target.value)}
                    className="w-full border border-input rounded-md px-3 py-2 text-sm font-mono"
                  />
                </label>

                {refundCents > (refundRow?.owed_cents ?? 0) ? (
                  <p className="text-red-700">Exceeds the {formatDollars(refundRow?.owed_cents ?? 0)} owed.</p>
                ) : preview && preview.units === 0 ? (
                  <p className="text-red-700">
                    {formatDollars(refundCents)} rounds down to 0 units at the current rate — refund more.
                  </p>
                ) : preview ? (
                  <div className="rounded-md border border-primary bg-primary/5 p-3 space-y-1 text-foreground">
                    <div className="font-medium">
                      <span className="inline-flex items-center gap-1">
                        <Coins className="w-4 h-4" /> {preview.gold} gold
                      </span>
                      {' + '}
                      <span className="inline-flex items-center gap-1">
                        <Fish className="w-4 h-4" /> {preview.koi} koi
                      </span>{' '}
                      will be credited to {refundRow?.user.display_name}.
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {formatDollars(refundCents)} = {preview.units} units at {rates.koi_to_cents_denominator} units per{' '}
                      {formatDollars(rates.koi_to_cents_numerator)}, rounded down. Gold comes back first, capped at the{' '}
                      {refundRow?.gold_refundable} gold they spent and haven't already had returned; the rest is koi.
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Their owed balance drops to {formatDollars((refundRow?.owed_cents ?? 0) - refundCents)}.
                    </div>
                  </div>
                ) : null}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={!refundValid || submitting}
              onClick={(e) => {
                e.preventDefault()
                submitRefund()
              }}
            >
              {submitting ? 'Refunding…' : `Refund ${preview?.gold ?? 0} gold + ${preview?.koi ?? 0} koi`}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={issueRow != null} onOpenChange={(open) => !open && setIssueRow(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Issue {formatDollars(issueRow?.owed_cents ?? 0)} to {issueRow?.user.display_name}?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>
                  This moves <strong>real money</strong>.{' '}
                  {issueRow?.has_active_card
                    ? `Their active card will be topped up by ${formatDollars(issueRow?.owed_cents ?? 0)}.`
                    : `A new card grant will be issued for ${formatDollars(issueRow?.owed_cents ?? 0)}.`}{' '}
                  No koi or gold is charged — they already paid for this balance.
                </p>
                <p>
                  Runs through the normal settle service as a background job, so the ratchet may cap the send if HCB
                  already holds more than the ledger expects. The ledger updates once HCB confirms. Real HCB writes are
                  gated by <code>HCB_ALLOW_WRITES</code> in non-production envs.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={submitting}
              onClick={(e) => {
                e.preventDefault()
                submitIssue()
              }}
            >
              {submitting ? 'Queueing…' : 'Issue funds'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

AdminProjectGrantsUnissuedFunds.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
