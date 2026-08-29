import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, router, Deferred } from '@inertiajs/react'
import type { ColumnDef } from '@tanstack/react-table'
import { Search, X, Plus, ArrowUpRight, ArrowDownRight, UserPlus } from 'lucide-react'
import AdminLayout from '@/layouts/AdminLayout'
import { Badge } from '@/components/admin/ui/badge'
import { Button } from '@/components/admin/ui/button'
import { Input } from '@/components/admin/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/admin/ui/select'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/admin/ui/popover'
import { DataTable } from '@/components/admin/DataTable'
import { DataTableSkeleton } from '@/components/admin/DataTableSkeleton'
import { Skeleton } from '@/components/admin/ui/skeleton'
import CurrencyToggle, { type Currency } from '@/components/admin/CurrencyToggle'
import UserSearchCombobox, { type UserOption } from '@/components/admin/UserSearchCombobox'
import { cn } from '@/lib/utils'
import type { PagyProps } from '@/types'

type Transaction = {
  id: number
  user: { id: number; display_name: string; avatar: string; email: string }
  actor: { id: number; display_name: string } | null
  amount: number
  reason: string
  description: string
  created_at: string
}

type Stats = { count: number; net: number; added: number; removed: number }

const ALL = '__all__'

function humanizeReason(reason: string): string {
  return reason.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function StatCell({ label, value, tone }: { label: string; value?: number; tone?: 'pos' | 'neg' }) {
  const sign = value != null && value > 0 && tone === 'pos' ? '+' : ''
  return (
    <div className="px-4 py-3">
      {value == null ? (
        <Skeleton className="my-0.5 h-7 w-16" />
      ) : (
        <p
          className={cn(
            'text-2xl font-semibold tabular-nums',
            tone === 'pos' && 'text-green-700',
            tone === 'neg' && 'text-red-700',
          )}
        >
          {sign}
          {value.toLocaleString()}
        </p>
      )}
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  )
}

export default function AdminKoiTransactionsIndex({
  transactions,
  user_id_filter,
  user_filter,
  search,
  reason_filter,
  reasons,
  pagy,
  stats,
  currency,
}: {
  transactions?: Transaction[]
  user_id_filter: string
  user_filter: UserOption | null
  search: string
  reason_filter: string
  reasons: string[]
  pagy?: PagyProps
  stats?: Stats
  currency: Currency
}) {
  const unit = currency === 'koi' ? 'koi' : 'gold'
  const [q, setQ] = useState(search)
  const [userPickerOpen, setUserPickerOpen] = useState(false)
  const didMount = useRef(false)

  // Build the canonical query for a server reload, dropping empty params so URLs stay clean.
  function paramsFor(overrides: Record<string, string | undefined> = {}) {
    const base: Record<string, string | undefined> = {
      currency,
      user_id: user_id_filter || undefined,
      search: q || undefined,
      reason: reason_filter || undefined,
      ...overrides,
    }
    return Object.fromEntries(Object.entries(base).filter(([, v]) => v))
  }

  // Reloads only the deferred ledger props (no skeleton flash, no scroll jump) and resets to page 1.
  function reload(overrides: Record<string, string | undefined> = {}) {
    router.get('/admin/koi_transactions', paramsFor(overrides), {
      // Refresh the ledger props plus the controlled filter values so the reason Select / badges
      // stay in sync with what the server actually filtered on.
      only: ['transactions', 'pagy', 'stats', 'reason_filter', 'search', 'user_id_filter', 'user_filter'],
      preserveState: true,
      preserveScroll: true,
      replace: true,
    })
  }

  function filterByUser(user: UserOption) {
    setUserPickerOpen(false)
    reload({ user_id: String(user.id) })
  }

  // Debounce the search box so we reload on a pause, not on every keystroke.
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true
      return
    }
    const t = setTimeout(() => reload({ search: q || undefined }), 300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  // Switching currency swaps the underlying model + reason set, so do a full navigation.
  function switchCurrency(c: Currency) {
    if (c === currency) return
    const params: Record<string, string> = { currency: c }
    if (user_id_filter) params.user_id = user_id_filter
    if (q) params.search = q
    router.get('/admin/koi_transactions', params)
  }

  const hasFilters = !!(q || reason_filter || user_id_filter)
  function clearFilters() {
    setQ('')
    router.get('/admin/koi_transactions', { currency }, { preserveScroll: true })
  }

  const columns = useMemo<ColumnDef<Transaction>[]>(
    () => [
      {
        accessorKey: 'user',
        header: 'User',
        cell: ({ row }) => {
          const u = row.original.user
          return (
            <div className="flex items-center gap-2.5">
              <img src={u.avatar} alt="" className="size-7 rounded-full object-cover shrink-0" loading="lazy" />
              <div className="min-w-0">
                <a
                  href={`/admin/users/${u.id}`}
                  className="font-medium hover:underline"
                  onClick={(e) => e.stopPropagation()}
                >
                  {u.display_name}
                </a>
                <p className="truncate text-xs text-muted-foreground">{u.email}</p>
              </div>
            </div>
          )
        },
      },
      {
        accessorKey: 'amount',
        header: 'Amount',
        cell: ({ row }) => {
          const positive = row.original.amount > 0
          const Arrow = positive ? ArrowUpRight : ArrowDownRight
          return (
            <span
              className={cn(
                'inline-flex items-center gap-1 font-medium tabular-nums',
                positive ? 'text-green-700' : 'text-red-700',
              )}
            >
              <Arrow className="size-3.5" />
              {positive ? '+' : ''}
              {row.original.amount} {unit}
            </span>
          )
        },
      },
      {
        accessorKey: 'reason',
        header: 'Reason',
        cell: ({ row }) => (
          <Badge variant={row.original.reason === 'admin_adjustment' ? 'secondary' : 'outline'}>
            {humanizeReason(row.original.reason)}
          </Badge>
        ),
      },
      {
        accessorKey: 'description',
        header: 'Description',
        cell: ({ row }) => (
          <span className="block max-w-xs truncate text-muted-foreground">{row.original.description}</span>
        ),
      },
      {
        accessorKey: 'actor',
        header: 'By',
        cell: ({ row }) => (
          <span className="text-muted-foreground">{row.original.actor?.display_name ?? 'System'}</span>
        ),
      },
      {
        accessorKey: 'created_at',
        header: 'Date',
        cell: ({ row }) => (
          <span className="whitespace-nowrap text-muted-foreground tabular-nums">{row.original.created_at}</span>
        ),
      },
    ],
    [unit],
  )

  const newPath = `/admin/koi_transactions/new?currency=${currency}${user_id_filter ? `&user_id=${user_id_filter}` : ''}`

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{unit === 'koi' ? 'Koi' : 'Gold'} ledger</h1>
          <p className="text-sm text-muted-foreground">Every {unit} awarded, spent, and adjusted.</p>
        </div>
        <div className="flex items-center gap-2">
          <CurrencyToggle value={currency} onChange={switchCurrency} />
          <Button asChild>
            <Link href={newPath}>
              <Plus className="size-4" />
              Adjust {unit === 'koi' ? 'koi' : 'gold'}
            </Link>
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap divide-x divide-border rounded-lg border border-border">
        <StatCell label="Transactions" value={stats?.count} />
        <StatCell label={`${unit} added`} value={stats?.added} tone="pos" />
        <StatCell label={`${unit} removed`} value={stats == null ? undefined : -stats.removed} tone="neg" />
        <StatCell label="Net balance" value={stats?.net} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by user, email, or description…"
            className="pl-9"
          />
        </div>
        <Select value={reason_filter || ALL} onValueChange={(v) => reload({ reason: v === ALL ? undefined : v })}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="Reason" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All reasons</SelectItem>
            {reasons.map((r) => (
              <SelectItem key={r} value={r}>
                {humanizeReason(r)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {user_id_filter ? (
          <Badge variant="secondary" className="h-9 gap-1.5 px-2.5">
            {user_filter?.avatar && (
              <img src={user_filter.avatar} alt="" className="size-4 rounded-full object-cover" />
            )}
            {user_filter?.display_name ?? `User #${user_id_filter}`}
            <button
              onClick={() => reload({ user_id: undefined })}
              className="hover:text-foreground"
              aria-label="Clear user filter"
            >
              <X className="size-3" />
            </button>
          </Badge>
        ) : (
          <Popover open={userPickerOpen} onOpenChange={setUserPickerOpen}>
            <PopoverTrigger asChild>
              <Button variant="outline" className="gap-1.5">
                <UserPlus className="size-4" />
                Filter by user
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-80 p-0" align="end">
              <UserSearchCombobox autoFocus onSelect={filterByUser} />
            </PopoverContent>
          </Popover>
        )}
        {hasFilters && (
          <Button variant="ghost" onClick={clearFilters}>
            <X className="size-4" />
            Clear
          </Button>
        )}
      </div>

      <Deferred
        data={['transactions', 'pagy']}
        fallback={
          <DataTableSkeleton
            columns={columns.length}
            headers={['User', 'Amount', 'Reason', 'Description', 'By', 'Date']}
            firstColumnVariant="avatar"
          />
        }
      >
        <DataTable columns={columns} data={transactions ?? []} pagy={pagy} noun="transactions" />
      </Deferred>
    </div>
  )
}

AdminKoiTransactionsIndex.layout = (page: ReactNode) => <AdminLayout>{page}</AdminLayout>
