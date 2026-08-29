import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { useForm, usePage, Link } from '@inertiajs/react'
import AdminLayout from '@/layouts/AdminLayout'
import { Button } from '@/components/admin/ui/button'
import { Card, CardContent } from '@/components/admin/ui/card'
import { Alert, AlertDescription } from '@/components/admin/ui/alert'
import type { SharedProps } from '@/types'

type LedgerData = {
  found: boolean
  user?: { id: number; display_name: string; email: string }
  has_card?: boolean
  // False when the user has no active card (e.g. it was cancelled after a
  // reimbursement). actual/expected are then $0-vs-nothing, so the gap math is
  // meaningless and its warnings are suppressed below.
  has_active_card?: boolean
  // actual = what HCB actually holds across this user's grant cards
  // expected = Fallout's ledger net (completed in-topups minus out-adjustments)
  actual_cents?: number
  expected_cents?: number
}

function formatDollars(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  return `${sign}$${(Math.abs(cents) / 100).toFixed(2)}`
}

function LedgerSnapshot({
  label,
  actual,
  expected,
  highlight,
}: {
  label: string
  actual: number
  expected: number
  highlight?: boolean
}) {
  const gap = actual - expected
  const gapLabel =
    gap === 0 ? 'match' : `${formatDollars(Math.abs(gap))} ${gap > 0 ? 'extra on HCB' : 'missing from HCB'}`
  return (
    <div className={`rounded-md border p-2.5 ${highlight ? 'border-primary bg-primary/5' : 'border-border'}`}>
      <div className="text-[11px] text-muted-foreground uppercase tracking-wide mb-1">{label}</div>
      <dl className="space-y-0.5 text-xs font-mono">
        <div className="flex justify-between" title="HCB's authoritative amount_cents — the real-world state">
          <dt className="text-muted-foreground">actual (HCB)</dt>
          <dd>{formatDollars(actual)}</dd>
        </div>
        <div
          className="flex justify-between"
          title="Fallout's ledger net (in minus out) — what we think should be there"
        >
          <dt className="text-muted-foreground">expected (ledger)</dt>
          <dd className={expected < 0 ? 'text-red-700' : ''}>{formatDollars(expected)}</dd>
        </div>
        <div className="flex justify-between border-t border-border pt-1 mt-1">
          <dt className="text-muted-foreground">gap</dt>
          <dd className={gap === 0 ? '' : 'text-red-700'}>{gapLabel}</dd>
        </div>
      </dl>
    </div>
  )
}

const CALC_INPUT_CLASS = 'w-full border border-input rounded-md px-2 py-1 text-xs font-mono'

// Prorates a partial card refund back into koi/gold. Works off the order's FROZEN units
// rather than today's rate — koi_to_cents_numerator may have moved since the order was
// placed, so converting the returned dollars at the current rate would refund the wrong
// amount. Floors the total (mirroring the ceil-on-charge convention in
// HcbGrantSetting#koi_for_usd_cents, so the program never over-refunds) and returns gold
// before koi, the inverse of the koi-first/gold-second charge order.
function KoiRefundCalculator() {
  const [frozenKoiInput, setFrozenKoiInput] = useState('')
  const [frozenGoldInput, setFrozenGoldInput] = useState('')
  const [orderDollars, setOrderDollars] = useState('')
  const [returnedDollars, setReturnedDollars] = useState('')

  const frozenKoi = Math.max(0, parseInt(frozenKoiInput, 10) || 0)
  const frozenGold = Math.max(0, parseInt(frozenGoldInput, 10) || 0)
  const orderCents = Math.round((parseFloat(orderDollars) || 0) * 100)
  const returnedCents = Math.round((parseFloat(returnedDollars) || 0) * 100)
  const totalUnits = frozenKoi + frozenGold

  const ready = orderCents > 0 && returnedCents > 0 && totalUnits > 0
  const overReturn = ready && returnedCents > orderCents
  const refundUnits = ready && !overReturn ? Math.floor((totalUnits * returnedCents) / orderCents) : 0
  const refundGold = Math.min(frozenGold, refundUnits)
  const refundKoi = refundUnits - refundGold

  return (
    <div className="rounded-md border border-border bg-background p-2.5 space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="block text-[11px] text-muted-foreground mb-0.5">Order koi charged</span>
          <input
            type="number"
            value={frozenKoiInput}
            onChange={(e) => setFrozenKoiInput(e.target.value)}
            placeholder="70"
            className={CALC_INPUT_CLASS}
          />
        </label>
        <label className="block">
          <span className="block text-[11px] text-muted-foreground mb-0.5">Order gold charged</span>
          <input
            type="number"
            value={frozenGoldInput}
            onChange={(e) => setFrozenGoldInput(e.target.value)}
            placeholder="0"
            className={CALC_INPUT_CLASS}
          />
        </label>
        <label className="block">
          <span className="block text-[11px] text-muted-foreground mb-0.5">Order total ($)</span>
          <input
            type="number"
            step="0.01"
            value={orderDollars}
            onChange={(e) => setOrderDollars(e.target.value)}
            placeholder="50.00"
            className={CALC_INPUT_CLASS}
          />
        </label>
        <label className="block">
          <span className="block text-[11px] text-muted-foreground mb-0.5">Returned to org ($)</span>
          <input
            type="number"
            step="0.01"
            value={returnedDollars}
            onChange={(e) => setReturnedDollars(e.target.value)}
            placeholder="30.00"
            className={CALC_INPUT_CLASS}
          />
        </label>
      </div>

      {overReturn ? (
        <p className="text-red-700">
          Returned amount is larger than the order total — check the figures. A card can hold top-ups from several
          orders; prorate against whichever order you're actually unwinding.
        </p>
      ) : refundUnits > 0 ? (
        <div className="rounded-md border border-primary bg-primary/5 p-2 space-y-1">
          <div className="font-mono">
            floor(({frozenKoi} + {frozenGold}) × {returnedCents} ÷ {orderCents}) = <strong>{refundUnits}</strong> units
          </div>
          <div>
            Credit <strong>{refundGold} gold</strong> and <strong>{refundKoi} koi</strong> on{' '}
            <Link href="/admin/koi_transactions/new" className="text-primary hover:underline">
              the koi/gold adjustment page
            </Link>
            .
          </div>
        </div>
      ) : (
        <p className="text-muted-foreground">Fill all four fields to compute the split.</p>
      )}
    </div>
  )
}

export default function AdminProjectGrantsAdjustmentsNew({
  prefill_user_id,
  idempotency_key,
}: {
  prefill_user_id: string
  idempotency_key: string
}) {
  const { errors } = usePage<SharedProps>().props
  const form = useForm({
    user_id: prefill_user_id,
    direction: 'in' as 'in' | 'out',
    amount_dollars: '',
    note: '',
    // Unchecked by default — admin must explicitly opt in to "this counts as
    // issued funding". Safer in both directions: if they forget, future orders
    // aren't accidentally reduced; if they check it, they've thought about it.
    counts_toward_funding: false,
    // One-shot token consumed server-side to block duplicate submits.
    idempotency_key,
  })

  // Debounced ledger fetch: as the admin types a user ID, we ask the server for that
  // user's current ledger. The projection (transferred ± amount) is computed client-side
  // from that snapshot + the direction/amount fields.
  const [ledger, setLedger] = useState<LedgerData | null>(null)
  const [ledgerLoading, setLedgerLoading] = useState(false)

  useEffect(() => {
    const id = form.data.user_id.trim()
    if (!id) {
      setLedger(null)
      return
    }
    setLedgerLoading(true)
    const handle = setTimeout(() => {
      fetch(`/admin/project_grants/adjustments/ledger?user_id=${encodeURIComponent(id)}`, {
        headers: { Accept: 'application/json' },
      })
        .then((r) => (r.ok ? r.json() : { found: false }))
        .then((data: LedgerData) => setLedger(data))
        .catch(() => setLedger({ found: false }))
        .finally(() => setLedgerLoading(false))
    }, 350)
    return () => clearTimeout(handle)
  }, [form.data.user_id])

  function submit(e: React.FormEvent) {
    e.preventDefault()
    form.post('/admin/project_grants/adjustments')
  }

  // Client-side projection. `in` raises expected (Fallout's ledger); `out` lowers it.
  // Actual is HCB's amount_cents — it never moves from an adjustment because an
  // adjustment is a ledger-only record of something that already happened on HCB.
  const amountCents = Math.round((parseFloat(form.data.amount_dollars) || 0) * 100)
  const canProject = ledger?.found && amountCents > 0
  const currentActual = ledger?.actual_cents ?? 0
  const currentExpected = ledger?.expected_cents ?? 0
  const projectedExpected = canProject
    ? currentExpected + (form.data.direction === 'in' ? amountCents : -amountCents)
    : currentExpected
  const currentGap = currentActual - currentExpected
  const projectedGap = currentActual - projectedExpected
  // Only meaningful when there's an active card behind the actual/expected figures.
  // With no active card (e.g. a post-reimbursement entry on a cancelled card) the
  // baseline is $0-vs-nothing, so the gap warnings are false alarms — suppress them.
  const hasActiveCard = ledger?.has_active_card !== false

  return (
    <div className="max-w-xl">
      <div className="mb-4">
        <Link href="/admin/project_grants/orders" className="text-sm text-primary hover:underline">
          ← Project Grants
        </Link>
      </div>
      <h1 className="text-2xl font-semibold tracking-tight mb-2">Manual ledger adjustment</h1>
      <p className="text-sm text-muted-foreground mb-4">
        Records an <code>in</code> or <code>out</code> ledger row without hitting HCB. Use this when real money has
        already moved outside the normal settle flow and the ledger is out of sync with reality.
      </p>

      <details className="mb-6 rounded-md border border-border bg-muted/30">
        <summary className="cursor-pointer px-4 py-2 text-sm font-medium select-none">
          How to compensate for unexpected movement
        </summary>
        <div className="px-4 pb-4 pt-1 text-xs space-y-3">
          <p>
            The ledger tracks <strong>movement on the user's HCB grant card</strong>. <code>transferred</code> = in rows
            − out rows. Fallout's settle service sends <code>expected − transferred</code> every time a new order is
            fulfilled — so as long as the ledger matches what's on the card, the next topup self-corrects.
          </p>

          <div>
            <div className="font-semibold mb-1">
              Use direction = in when money landed on the card outside of Fallout.
            </div>
            <ul className="list-disc list-inside space-y-1 text-muted-foreground">
              <li>
                <strong>You topped up the card manually on the HCB dashboard.</strong> Fallout didn't record it — add an{' '}
                <code>in</code> row so the ledger knows the money is on the card (otherwise the next order will double
                up).
              </li>
              <li>
                <strong>Someone else granted the card outside Fallout.</strong> Same story — record what actually hit
                the card.
              </li>
              <li>
                <strong>After an invoice was paid, you manually topped up the card.</strong> Invoices don't hit the card
                directly — they flow to the org's bank account, and you then move that money onto the card via HCB.
                Record the <em>card top-up</em> here, not the invoice.
              </li>
              <li>
                <strong>Warning shows ledger_divergence with card balance &gt; ledger net.</strong> If investigation
                confirms the card really does hold more, book an <code>in</code> for the difference and resolve the
                warning.
              </li>
            </ul>
          </div>

          <div>
            <div className="font-semibold mb-1">
              Use direction = out when money came off the card outside of Fallout.
            </div>
            <ul className="list-disc list-inside space-y-1 text-muted-foreground">
              <li>
                <strong>You withdrew from the card on the HCB dashboard.</strong> Funds moved off the card back to the
                org — add an <code>out</code> row so Fallout stops counting it as transferred.
              </li>
              <li>
                <strong>HCB card was canceled with a balance on it.</strong> Cancelation returns any remaining balance
                to the org — record an <code>out</code> for that residual.
              </li>
              <li>
                <strong>Order was rejected but money already left.</strong> If you can pull it back off the card on HCB,
                record that withdrawal as <code>out</code>. If the funds were already spent and can't be recovered,{' '}
                <em>don't record anything</em> — <code>expected</code> dropping below <code>transferred</code> on its
                own is the correct over-transfer signal (Sentry warns; future orders send less until delta goes positive
                again).
              </li>
            </ul>
          </div>

          <div>
            <div className="font-semibold mb-1">What about a duplicate topup that we can't recover?</div>
            <p className="text-muted-foreground">
              If duplicate money landed on the card and the user is keeping it, book an <code>in</code> for the
              duplicate amount. That raises <code>transferred</code> to match reality; future orders will send less
              because <code>delta</code> is already capped by the extra on-card balance.
            </p>
          </div>

          <div className="pt-1 border-t border-border">
            <div className="font-semibold mb-1">Workflow</div>
            <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
              <li>Verify the real-world state on the HCB dashboard first — what's actually on the card?</li>
              <li>Compare to the user's ledger on the order show page (expected / transferred / delta).</li>
              <li>
                Decide: did money move <em>onto</em> the card (<code>in</code>) or <em>off</em> the card (
                <code>out</code>)?
              </li>
              <li>Record the adjustment here with a clear note citing the HCB transaction.</li>
              <li>
                If a warning surfaced the issue, return to <code>/admin/project_grants/orders</code> and resolve it.
              </li>
            </ol>
          </div>
        </div>
      </details>

      <details className="mb-6 rounded-md border border-border bg-muted/30">
        <summary className="cursor-pointer px-4 py-2 text-sm font-medium select-none">
          How to handle a reimbursement
        </summary>
        <div className="px-4 pb-4 pt-1 text-xs space-y-3">
          <p>
            An HCB <strong>reimbursement</strong> pays a user a fixed amount for an out-of-pocket expense and then{' '}
            <strong>cancels their grant card</strong>. The reimbursement lives entirely on HCB — to Fallout it is{' '}
            <strong>indistinguishable from a normal card cancelation</strong>. There's no transaction row and no signal,
            so it can't be automated. You compensate by hand here.
          </p>

          <div>
            <div className="font-semibold mb-1">Why a reimbursement needs a manual entry</div>
            <p className="text-muted-foreground">
              On cancelation, Fallout auto-books an <code>out</code> row for the card's unspent balance and{' '}
              <strong>replenishes the user's funding</strong> (so a future order re-sends what came back). But a
              reimbursement was <em>spent</em>, not returned — and since it's invisible, the auto-refund wrongly hands
              that money back as spendable funding. Left alone, the user is reimbursed <em>and</em> keeps the
              entitlement: the same dollars counted twice.
            </p>
          </div>

          <div>
            <div className="font-semibold mb-1">The fix: one compensating in adjustment</div>
            <p className="text-muted-foreground mb-1">
              <strong>After</strong> the cancelation has synced (≤15 min — the auto-refund must land first), and{' '}
              <strong>before</strong> issuing the user any replacement card, record:
            </p>
            <ul className="list-disc list-inside space-y-1 text-muted-foreground">
              <li>
                Direction = <code>in</code>
              </li>
              <li>
                Amount = the <strong>reimbursed</strong> amount
              </li>
              <li>
                <strong>Count towards issued funding = checked</strong>
              </li>
              <li>Note citing the HCB reimbursement report and the cancelled card</li>
            </ul>
            <p className="text-muted-foreground mt-1">
              An <code>in</code> with funding checked is the only lever that pushes entitlement back <em>down</em> — it
              reverses the slice of the auto-refund that was actually spent. An <code>out</code> would do the opposite
              (it's a refund), and an unchecked row wouldn't touch funding at all.
            </p>
          </div>

          <div>
            <div className="font-semibold mb-1">Example — $50 card, $30 reimbursed, $20 genuinely unspent</div>
            <p className="text-muted-foreground">
              Auto-refund returns the full $50 as funding (wrong — over by $30). You book <code>in</code> $30, funding
              checked. Now the user is left with exactly <strong>$20</strong> of entitlement for their next card, and
              the $30 reimbursement is correctly recorded as spent.
            </p>
          </div>

          <p className="text-muted-foreground italic">
            Heads up: the preview below only measures the user's <strong>active</strong> cards, and the reimbursed card
            is now cancelled — so it shows a $0 baseline and will flag a misleading "creates a gap" / "missing from HCB"
            warning for this entry. <strong>Disregard it.</strong> A closed card legitimately diverges from the ledger
            and isn't tracked; the adjustment still records and settles correctly.
          </p>
        </div>
      </details>

      <details className="mb-6 rounded-md border border-border bg-muted/30">
        <summary className="cursor-pointer px-4 py-2 text-sm font-medium select-none">
          How to refund an unspent card balance back into koi/gold
        </summary>
        <div className="px-4 pb-4 pt-1 text-xs space-y-3">
          <p>
            A user got a card, spent part of it, and wants the rest back as <strong>koi/gold</strong> instead of
            dollars. There's no automated path — the dollars and the koi live in two separate ledgers, and each half is
            a manual step on a different page. Do all three, in order.
          </p>

          <div>
            <div className="font-semibold mb-1">The trap: refunding both halves pays the user twice</div>
            <p className="text-muted-foreground">
              When a card closes, Fallout auto-books an <code>out</code> row that{' '}
              <strong>replenishes the user's funding</strong> — the returned dollars stay spendable on their next order.
              That's right when the money stays in USD. Here it isn't: you're handing those same dollars back as koi.
              Leave the auto-refund alone and the user gets the koi <em>and</em> the entitlement.
            </p>
          </div>

          <div>
            <div className="font-semibold mb-1">1. Move the real money on HCB</div>
            <p className="text-muted-foreground">
              Nothing in Fallout initiates this — do it on the HCB dashboard. Either <strong>cancel the card</strong>{' '}
              (returns the whole unspent balance; irreversible, so issue a new card if they still need to spend) or{' '}
              <strong>withdraw part of it</strong>. Cancel is the well-trodden path.
            </p>
          </div>

          <div>
            <div className="font-semibold mb-1">2. Cancel the entitlement (this form)</div>
            <p className="text-muted-foreground mb-1">
              <strong>If you cancelled:</strong> wait for the auto-refund to land (≤15 min — its note records the exact
              unspent figure), then book the mirror of it here:
            </p>
            <ul className="list-disc list-inside space-y-1 text-muted-foreground">
              <li>
                Direction = <code>in</code>
              </li>
              <li>
                Amount = the <strong>returned</strong> balance (same figure as the auto-booked <code>out</code>)
              </li>
              <li>
                <strong>Count towards issued funding = checked</strong>
              </li>
              <li>Note citing the order being unwound and the koi/gold credit from step 3</li>
            </ul>
            <p className="text-muted-foreground mt-1">
              That nets the pair to zero, so the returned dollars don't come back as spendable funding.{' '}
              <strong>If you withdrew instead of cancelling</strong>, nothing auto-books — record a single{' '}
              <code>out</code> for the withdrawn amount with <strong>count towards issued funding unchecked</strong>.
              Same net effect in one row: the card ledger matches HCB, the entitlement doesn't move.
            </p>
          </div>

          <div>
            <div className="font-semibold mb-1">3. Credit the koi/gold</div>
            <p className="text-muted-foreground mb-2">
              Leave the order <code>fulfilled</code> — rejecting it refunds <em>all</em> of its koi and pulls the full
              amount out of <code>expected</code>, which cascades into more corrections. Prorate instead, off the units
              the order actually froze:
            </p>
            <KoiRefundCalculator />
            <p className="text-muted-foreground mt-2">
              Gold comes back before koi (the inverse of the koi-first charge order), and the total floors so the
              program never over-refunds. Cite the order id, the card, and the dollar amount in the description so the
              two ledgers can be reconciled later.
            </p>
          </div>

          <p className="text-muted-foreground italic">
            Heads up: steps 1–2 need the <code>hcb</code> role, step 3 only needs <code>admin</code> — nothing ties them
            together, so a half-finished refund won't warn you. If the card was cancelled, the preview below also shows
            a $0 baseline and a misleading gap warning; disregard it, same as for a reimbursement.
          </p>
        </div>
      </details>

      {Object.keys(errors).length > 0 && (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription>
            {Object.values(errors)
              .flat()
              .map((msg, i) => (
                <p key={i}>{msg}</p>
              ))}
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardContent className="pt-6">
          <form onSubmit={submit} className="space-y-4">
            <label className="block">
              <span className="block text-sm font-medium mb-1.5">User ID</span>
              <input
                type="number"
                value={form.data.user_id}
                onChange={(e) => form.setData('user_id', e.target.value)}
                required
                className="w-full border border-input rounded-md px-3 py-2 text-sm"
                placeholder="User ID (find on /admin/users)"
              />
              <p className="text-xs text-muted-foreground mt-1">User must already have an HCB grant card on record.</p>
              {form.data.user_id && ledger && !ledger.found && (
                <p className="text-xs text-red-700 mt-1">No user with ID {form.data.user_id}.</p>
              )}
              {ledger?.found && ledger.user && (
                <p className="text-xs text-muted-foreground mt-1">
                  ✓ {ledger.user.display_name} ({ledger.user.email})
                  {!ledger.has_card && (
                    <span className="text-red-700"> — no HCB grant card on record; save will fail</span>
                  )}
                </p>
              )}
              {ledgerLoading && <p className="text-xs text-muted-foreground mt-1">Loading ledger…</p>}
            </label>

            <label className="block">
              <span className="block text-sm font-medium mb-1.5">Direction</span>
              <select
                value={form.data.direction}
                onChange={(e) => form.setData('direction', e.target.value as 'in' | 'out')}
                required
                className="w-full border border-input rounded-md px-3 py-2 text-sm bg-background"
              >
                <option value="in">in — money landed on the card (you topped it up on HCB manually)</option>
                <option value="out">out — money came off the card (you withdrew on HCB manually)</option>
              </select>
            </label>

            <label className="block">
              <span className="block text-sm font-medium mb-1.5">Amount (USD)</span>
              <input
                type="number"
                step="0.01"
                min="0.01"
                value={form.data.amount_dollars}
                onChange={(e) => form.setData('amount_dollars', e.target.value)}
                required
                className="w-full border border-input rounded-md px-3 py-2 text-sm font-mono"
                placeholder="25.00"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Always a positive number. Direction determines sign in the ledger.
              </p>
            </label>

            <label className="flex items-start gap-2 p-2 rounded-md border border-border bg-muted/20">
              <input
                type="checkbox"
                checked={form.data.counts_toward_funding}
                onChange={(e) => form.setData('counts_toward_funding', e.target.checked)}
                className="mt-0.5"
              />
              <span className="text-sm">
                <span className="font-medium">Count towards issued funding</span>
                <span className="block text-xs text-muted-foreground mt-0.5">
                  Check this if the movement was funded by the project funding program (e.g. you topped up HCB by hand
                  because an auto-settle failed). Future order topups will be reduced by this amount. Leave unchecked
                  for out-of-band HCB activity (someone else credited the card, unrelated disbursement, etc.) — the
                  ledger still records it but it won't offset future order amounts.
                </span>
              </span>
            </label>

            <label className="block">
              <span className="block text-sm font-medium mb-1.5">Note (required)</span>
              <textarea
                value={form.data.note}
                onChange={(e) => form.setData('note', e.target.value)}
                required
                rows={3}
                className="w-full border border-input rounded-md px-3 py-2 text-sm"
                placeholder="Why this adjustment? e.g. 'topped up $25 on HCB manually after order #12 — ledger was $25 short'"
              />
            </label>

            {ledger?.found && (
              <div className="rounded-md border border-border bg-muted/20 p-3 space-y-3">
                <div className="text-xs font-semibold">Ledger preview for {ledger.user?.display_name}</div>
                {canProject ? (
                  <>
                    <div className="grid grid-cols-[1fr_auto_1fr] gap-2 items-stretch">
                      <LedgerSnapshot label="Current" actual={currentActual} expected={currentExpected} />
                      <div className="flex flex-col items-center justify-center text-xs text-muted-foreground px-1">
                        <div className="text-[10px] uppercase tracking-wide">{form.data.direction}</div>
                        <div
                          className={`font-mono font-semibold ${form.data.direction === 'out' ? 'text-red-700' : 'text-green-700'}`}
                        >
                          {form.data.direction === 'out' ? '−' : '+'}
                          {formatDollars(amountCents)}
                        </div>
                        <div className="text-sm">→</div>
                      </div>
                      <LedgerSnapshot
                        label="After this adjustment"
                        actual={currentActual}
                        expected={projectedExpected}
                        highlight
                      />
                    </div>
                    <div className="text-xs text-muted-foreground space-y-1">
                      <p>
                        <strong>What this changes:</strong> <code>expected</code> (Fallout ledger){' '}
                        {form.data.direction === 'in' ? 'rises' : 'falls'} by {formatDollars(amountCents)}.{' '}
                        <code>actual</code> (HCB) is unchanged — adjustments record movement that already happened on
                        HCB, they don't call the API.
                      </p>
                      {hasActiveCard ? (
                        <>
                          {currentGap !== 0 && projectedGap === 0 && (
                            <p className="text-green-700">
                              <strong>This adjustment closes the gap.</strong> After it lands, Fallout's ledger matches
                              HCB exactly.
                            </p>
                          )}
                          {currentGap === 0 && projectedGap !== 0 && (
                            <p className="text-red-700">
                              <strong>⚠ This will create a {formatDollars(Math.abs(projectedGap))} gap.</strong> Ledger
                              and HCB currently match; this adjustment would push them out of sync. Only proceed if an
                              out-of-band HCB event actually happened.
                            </p>
                          )}
                          {currentGap !== 0 && projectedGap !== 0 && Math.abs(projectedGap) > Math.abs(currentGap) && (
                            <p className="text-red-700">
                              <strong>⚠ Gap grows:</strong> {formatDollars(Math.abs(currentGap))} →{' '}
                              {formatDollars(Math.abs(projectedGap))}. This adjustment moves the ledger further from
                              HCB, not closer. Double-check direction and amount.
                            </p>
                          )}
                        </>
                      ) : (
                        <p>
                          <strong>No active card to compare against.</strong> This user's grant card is closed, so the
                          actual-vs-ledger gap above is measured against $0 and isn't meaningful — ignore it. The row
                          still records and settles correctly. (Expected when compensating for a reimbursement.)
                        </p>
                      )}
                      {hasActiveCard && projectedExpected < 0 && (
                        <p className="text-red-700">
                          <strong>⚠ expected would go negative.</strong> That means more out-adjustments than in-topups
                          on record — almost always a mistake.
                        </p>
                      )}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="grid grid-cols-1">
                      <LedgerSnapshot label="Current" actual={currentActual} expected={currentExpected} />
                    </div>
                    <p className="text-xs text-muted-foreground italic">
                      Enter an amount above to see how this adjustment will change the ledger.
                    </p>
                  </>
                )}
              </div>
            )}

            <div className="rounded-md border border-yellow-500 bg-yellow-50 dark:bg-yellow-950/20 p-3 text-xs">
              <strong>⚠ This only changes Fallout's ledger.</strong> It does not call the HCB API. Double-check that the
              real-world money movement has already happened before saving. Ledger rows are immutable once created.
            </div>

            <div className="flex gap-2">
              <Button type="submit" disabled={form.processing}>
                {form.processing ? 'Saving…' : 'Record adjustment'}
              </Button>
              <Button type="button" variant="outline" asChild>
                <Link href="/admin/project_grants/orders">Cancel</Link>
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}

AdminProjectGrantsAdjustmentsNew.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
