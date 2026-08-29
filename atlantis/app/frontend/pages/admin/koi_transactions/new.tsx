import { useState, type ReactNode } from 'react'
import { router, Link } from '@inertiajs/react'
import { ArrowLeft, X, Fish, Coins } from 'lucide-react'
import AdminLayout from '@/layouts/AdminLayout'
import { Button } from '@/components/admin/ui/button'
import { Input } from '@/components/admin/ui/input'
import { Textarea } from '@/components/admin/ui/textarea'
import { Alert, AlertDescription } from '@/components/admin/ui/alert'
import { Badge } from '@/components/admin/ui/badge'
import CurrencyToggle, { type Currency } from '@/components/admin/CurrencyToggle'
import UserSearchCombobox, { type UserOption } from '@/components/admin/UserSearchCombobox'
import { cn } from '@/lib/utils'

const QUICK_AMOUNTS = [5, 10, 25, 50, -10]

export default function AdminKoiTransactionsNew({
  prefill_user,
  currency,
}: {
  prefill_user: UserOption | null
  currency: Currency
}) {
  const unit = currency === 'koi' ? 'koi' : 'gold'
  const Icon = currency === 'koi' ? Fish : Coins

  const [selected, setSelected] = useState<UserOption | null>(prefill_user)
  const [amount, setAmount] = useState('')
  const [description, setDescription] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [errors, setErrors] = useState<Record<string, string[]>>({})

  function switchCurrency(c: Currency) {
    if (c === currency) return
    const params: Record<string, string> = { currency: c }
    if (selected) params.user_id = String(selected.id)
    router.get('/admin/koi_transactions/new', params)
  }

  const parsedAmount = parseInt(amount, 10)
  const validAmount = Number.isInteger(parsedAmount) && parsedAmount !== 0
  const currentBalance = selected ? selected[currency] : 0
  const newBalance = currentBalance + (validAmount ? parsedAmount : 0)

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!selected || !validAmount) return
    setSubmitting(true)
    setErrors({})

    const key = currency === 'gold' ? 'gold_transaction' : 'koi_transaction'
    router.post(
      `/admin/koi_transactions?currency=${currency}`,
      { [key]: { user_id: selected.id, amount: parsedAmount, description } },
      {
        onError: (errs: Record<string, string[]>) => {
          setSubmitting(false)
          setErrors(errs)
        },
        onSuccess: () => setSubmitting(false),
      },
    )
  }

  const backPath = `/admin/koi_transactions?currency=${currency}${selected ? `&user_id=${selected.id}` : ''}`

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <div>
        <Link
          href={backPath}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" /> Back to ledger
        </Link>
        <div className="mt-2 flex items-center justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">Adjust {unit === 'koi' ? 'koi' : 'gold'}</h1>
          <CurrencyToggle value={currency} onChange={switchCurrency} />
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Adds a ledger entry. Positive amounts grant {unit}; negative amounts deduct it.
        </p>
      </div>

      <details className="rounded-md border border-border bg-muted/30">
        <summary className="cursor-pointer select-none px-4 py-2 text-sm font-medium">
          Refunding an unspent project grant card?
        </summary>
        <div className="space-y-2 px-4 pb-4 pt-1 text-xs text-muted-foreground">
          <p>
            Crediting {unit} here is only <strong>step 3</strong> of that process. The dollars have to come off the card
            on HCB first, and the user's USD entitlement has to be cancelled on the{' '}
            <Link href="/admin/project_grants/adjustments/new" className="text-primary hover:underline">
              grant ledger adjustment page
            </Link>{' '}
            — skip that and the same money is refunded twice, once as {unit} and once as spendable funding.
          </p>
          <p>
            That page also carries the full runbook and a calculator for the prorated split. Don't convert the returned
            dollars at today's rate: prorate off the units the order froze, since the koi rate may have moved since.
          </p>
        </div>
      </details>

      {Object.keys(errors).length > 0 && (
        <Alert variant="destructive">
          <AlertDescription>
            {Object.entries(errors).map(([field, msgs]) => (
              <p key={field}>
                <strong className="capitalize">{field.replace(/_/g, ' ')}:</strong>{' '}
                {Array.isArray(msgs) ? msgs.join(', ') : String(msgs)}
              </p>
            ))}
          </AlertDescription>
        </Alert>
      )}

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* User picker */}
        <div className="space-y-1.5">
          <label className="text-sm font-medium">User</label>
          {!selected ? (
            <div className="overflow-hidden rounded-md border border-border">
              <UserSearchCombobox autoFocus onSelect={setSelected} />
            </div>
          ) : (
            <div className="flex items-start gap-3 rounded-md border border-border p-3">
              <img src={selected.avatar} alt="" className="size-12 rounded-full object-cover shrink-0" loading="lazy" />
              <div className="min-w-0 flex-1">
                <a href={`/admin/users/${selected.id}`} className="font-medium hover:underline">
                  {selected.display_name}
                </a>
                <div className="truncate text-xs text-muted-foreground">{selected.email}</div>
                <div className="mt-1.5 flex items-center gap-2 text-xs">
                  <Badge variant="outline" className="gap-1 font-normal">
                    <Fish className="size-3" /> {selected.koi} koi
                  </Badge>
                  <Badge variant="outline" className="gap-1 font-normal">
                    <Coins className="size-3" /> {selected.gold} gold
                  </Badge>
                </div>
              </div>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                onClick={() => setSelected(null)}
                title="Change user"
              >
                <X className="size-3.5" />
              </Button>
            </div>
          )}
        </div>

        {/* Amount */}
        <div className="space-y-1.5">
          <label htmlFor="amount" className="text-sm font-medium">
            Amount
          </label>
          <Input
            id="amount"
            type="number"
            inputMode="numeric"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="e.g. 25 to grant, -10 to deduct"
          />
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {QUICK_AMOUNTS.map((n) => (
              <button
                key={n}
                type="button"
                onClick={() => setAmount(String(n))}
                className={cn(
                  'rounded-md border px-2 py-1 text-xs font-medium tabular-nums transition-colors',
                  n > 0
                    ? 'border-green-700/30 text-green-700 hover:bg-green-700/10'
                    : 'border-red-700/30 text-red-700 hover:bg-red-700/10',
                )}
              >
                {n > 0 ? `+${n}` : n}
              </button>
            ))}
          </div>
        </div>

        {/* Balance preview */}
        {selected && validAmount && (
          <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-4 py-3 text-sm">
            <span className="inline-flex items-center gap-1.5 text-muted-foreground">
              <Icon className="size-4" /> New {unit} balance
            </span>
            <span className="inline-flex items-center gap-2 tabular-nums">
              <span className="text-muted-foreground">{currentBalance.toLocaleString()}</span>
              <span className="text-muted-foreground">→</span>
              <span className={cn('font-semibold', parsedAmount > 0 ? 'text-green-700' : 'text-red-700')}>
                {newBalance.toLocaleString()}
              </span>
            </span>
          </div>
        )}

        {/* Description */}
        <div className="space-y-1.5">
          <label htmlFor="description" className="text-sm font-medium">
            Reason / note
          </label>
          <Textarea
            id="description"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Why this adjustment? (shown in the ledger)"
          />
        </div>

        <div className="flex gap-2">
          <Button type="submit" disabled={!selected || !validAmount || !description.trim() || submitting}>
            {submitting ? 'Saving…' : 'Save adjustment'}
          </Button>
          <Button asChild type="button" variant="outline">
            <Link href={backPath}>Cancel</Link>
          </Button>
        </div>
      </form>
    </div>
  )
}

AdminKoiTransactionsNew.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
