import { useEffect, useRef, useState } from 'react'
import { Command } from 'cmdk'
import { Search, Fish, Coins } from 'lucide-react'
import { Skeleton } from '@/components/admin/ui/skeleton'

export type UserOption = {
  id: number
  display_name: string
  avatar: string
  email: string
  koi: number
  gold: number
}

function ResultSkeleton() {
  // No wrapper gap — rows stack exactly like the real Command.Item rows (which have none),
  // and each row mirrors an item (size-9 avatar + name line + sub-line) so heights match.
  return (
    <>
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-center gap-3 px-2 py-2">
          <Skeleton className="size-9 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-32" />
            <Skeleton className="h-3 w-40" />
          </div>
          <Skeleton className="h-7 w-10 shrink-0" />
        </div>
      ))}
    </>
  )
}

// Debounced user-search combobox backed by koi_transactions#users_search (verified users only,
// raw id lookup supported). Shared by the ledger user filter and the adjustment form.
export default function UserSearchCombobox({
  onSelect,
  autoFocus,
  placeholder = 'Search by name, email, or user ID…',
}: {
  onSelect: (user: UserOption) => void
  autoFocus?: boolean
  placeholder?: string
}) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<UserOption[]>([])
  const [searching, setSearching] = useState(false)

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (debounceTimer.current) clearTimeout(debounceTimer.current)
    abortRef.current?.abort()

    const trimmed = query.trim()
    if (trimmed.length === 0) {
      setResults([])
      setSearching(false)
      return
    }

    setSearching(true)
    debounceTimer.current = setTimeout(() => {
      const ctrl = new AbortController()
      abortRef.current = ctrl
      fetch(`/admin/koi_transactions/users_search?q=${encodeURIComponent(trimmed)}`, {
        headers: { Accept: 'application/json' },
        signal: ctrl.signal,
      })
        .then((r) => r.json())
        .then((data: { users: UserOption[] }) => {
          setResults(data.users ?? [])
          setSearching(false)
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === 'AbortError') return
          setSearching(false)
        })
    }, 250)

    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current)
    }
  }, [query])

  return (
    <Command shouldFilter={false}>
      <div className="flex items-center border-b border-border px-3">
        <Search className="mr-2 size-4 shrink-0 text-muted-foreground" />
        <Command.Input
          autoFocus={autoFocus}
          value={query}
          onValueChange={setQuery}
          placeholder={placeholder}
          className="flex-1 bg-transparent py-2.5 text-sm outline-none placeholder:text-muted-foreground"
        />
      </div>
      <Command.List className="max-h-64 overflow-y-auto p-1.5">
        {query.trim() === '' && (
          <div className="py-6 text-center text-sm text-muted-foreground">Type a name, email, or user ID.</div>
        )}
        {query.trim() !== '' && searching && <ResultSkeleton />}
        {query.trim() !== '' && !searching && results.length === 0 && (
          <div className="py-6 text-center text-sm text-muted-foreground">No matches.</div>
        )}
        {!searching &&
          results.map((u) => (
            <Command.Item
              key={u.id}
              value={`${u.id}-${u.display_name}`}
              onSelect={() => onSelect(u)}
              className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-2 text-foreground aria-selected:bg-accent aria-selected:text-accent-foreground"
            >
              <img src={u.avatar} alt="" className="size-9 shrink-0 rounded-full object-cover" loading="lazy" />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{u.display_name}</div>
                <div className="truncate text-xs text-muted-foreground">{u.email}</div>
              </div>
              <div className="shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                <div className="inline-flex items-center gap-1">
                  <Fish className="size-3" /> {u.koi}
                </div>
                <div className="inline-flex items-center gap-1">
                  <Coins className="size-3" /> {u.gold}
                </div>
              </div>
            </Command.Item>
          ))}
      </Command.List>
    </Command>
  )
}
